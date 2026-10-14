import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from studio.saat.audio import GameAudio
from test_review import make_wav


class FakeSaatClient:
    calls = []
    result = None
    cancel_during_export = False

    def __init__(self, game, exe_sfx=None, exe_stream=None, timeout=3600):
        self.game = Path(game)
        self.proc = None

    def exportar_sfx(self, pak, saida, on_line=None, cancel=None, source=None, bank=None):
        self.__class__.calls.append((pak, source))
        out = Path(saida) / pak / 'Bank_001' / 'sound_001.wav'
        out.parent.mkdir(parents=True, exist_ok=True)
        make_wav(out)
        if self.cancel_during_export and cancel is not None:
            cancel.set()
        return dict(self.result) if self.result is not None else {
            'ok': True, 'rc': 0, 'cmd': ['fake', pak], 'log': [], 'wavs': 1,
        }


class SaatArchiveTests(unittest.TestCase):
    def setUp(self):
        FakeSaatClient.calls = []
        FakeSaatClient.result = None
        FakeSaatClient.cancel_during_export = False

    def _game(self, root):
        game_dir = root / 'game'
        sfx = game_dir / 'audio' / 'SFX'
        cfg = game_dir / 'audio' / 'CONFIG'
        sfx.mkdir(parents=True)
        cfg.mkdir(parents=True)
        for name in ('SCRIPT', 'SPC_EA'):
            (sfx / name).write_bytes(('base-' + name).encode())
        bank = cfg / 'BankLkup.dat'
        bank.write_bytes(b'base-bank')
        mod = game_dir / 'modloader' / 'ModA'
        mod_sfx = mod / 'audio' / 'SFX'
        (mod_sfx).mkdir(parents=True)
        (mod / 'audio' / 'CONFIG').mkdir(parents=True)
        (mod_sfx / 'SCRIPT').write_bytes(b'mod-script')
        (mod / 'audio' / 'CONFIG' / 'BankLkup.dat').write_bytes(b'mod-bank')
        info = {'ok': True, 'pasta': game_dir, 'script': sfx / 'SCRIPT', 'banklkup': bank,
                'modloader': game_dir / 'modloader', 'faltando': []}
        game = GameAudio({'game_dir': str(game_dir), 'modloader_mod': 'ModA'}, lambda: None,
                         root / 'cache', root / 'backup', root / 'manifest.json')
        game.info = info
        return game, game_dir, mod

    def test_script_then_spc_preserves_both_exports_and_cache_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game, game_dir, _mod = self._game(root)
            with patch('studio.saat.audio.SaatClient', FakeSaatClient), patch.object(
                    game, 'saat', return_value={'sfx': game_dir / 'saat_sfx.exe', 'stream': None}):
                (game_dir / 'saat_sfx.exe').touch()
                self.assertTrue(game.indexar('SCRIPT')['ok'])
                self.assertTrue(game.indexar('SPC_EA')['ok'])
            for pak in ('SCRIPT', 'SPC_EA'):
                self.assertTrue((game.cache_dir() / 'export' / pak / 'Bank_001' / 'sound_001.wav').is_file())
                self.assertTrue(game._cache_original_atual(pak))
            self.assertEqual({x[0] for x in FakeSaatClient.calls}, {'SCRIPT', 'SPC_EA'})

    def test_cache_hit_for_one_archive_does_not_skip_another(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game, game_dir, _mod = self._game(root)
            (game_dir / 'saat_sfx.exe').touch()
            with patch('studio.saat.audio.SaatClient', FakeSaatClient), patch.object(
                    game, 'saat', return_value={'sfx': game_dir / 'saat_sfx.exe', 'stream': None}):
                game.indexar('SCRIPT')
                self.assertTrue(game.indexar('SCRIPT')['pulado'])
                self.assertTrue(game.indexar('SPC_EA')['ok'])
            self.assertEqual([call[0] for call in FakeSaatClient.calls], ['SCRIPT', 'SPC_EA'])

    def test_changed_requested_source_invalidates_only_that_pak_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game, game_dir, _mod = self._game(root)
            (game_dir / 'saat_sfx.exe').touch()
            with patch('studio.saat.audio.SaatClient', FakeSaatClient), patch.object(
                    game, 'saat', return_value={'sfx': game_dir / 'saat_sfx.exe', 'stream': None}):
                game.indexar('SCRIPT')
                game.indexar('SPC_EA')
                old_spc_meta = game._cache_meta_path('SPC_EA').read_bytes()
                (game_dir / 'audio' / 'SFX' / 'SCRIPT').write_bytes(b'changed script source')
                refreshed = game.indexar('SCRIPT')
                self.assertTrue(refreshed['ok'])
                self.assertTrue(game.indexar('SPC_EA')['pulado'])
            self.assertEqual([call[0] for call in FakeSaatClient.calls], ['SCRIPT', 'SPC_EA', 'SCRIPT'])
            self.assertEqual(game._cache_meta_path('SPC_EA').read_bytes(), old_spc_meta)

    def test_cancel_and_nonzero_exit_preserve_previous_export_and_metadata(self):
        for result, cancel in (({'ok': False, 'rc': -1, 'cmd': [], 'log': [], 'wavs': 0, 'cancelado': True},
                                threading.Event()),
                               ({'ok': False, 'rc': 1, 'cmd': [], 'log': [], 'wavs': 1}, None)):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                game, game_dir, _mod = self._game(root)
                (game_dir / 'saat_sfx.exe').touch()
                old = game.cache_dir() / 'export' / 'SCRIPT' / 'Bank_001' / 'sound_001.wav'
                old.parent.mkdir(parents=True)
                make_wav(old)
                meta = game._cache_meta_path('SCRIPT')
                meta.parent.mkdir(parents=True, exist_ok=True)
                meta.write_text(json.dumps({'pak': 'SCRIPT', 'fonte': game._assinatura_fonte('SCRIPT')}),
                                encoding='utf-8')
                before_bytes, before_meta = old.read_bytes(), meta.read_bytes()
                if cancel is not None:
                    cancel.set()
                FakeSaatClient.result = result
                with patch('studio.saat.audio.SaatClient', FakeSaatClient), patch.object(
                        game, 'saat', return_value={'sfx': game_dir / 'saat_sfx.exe', 'stream': None}):
                    outcome = game.indexar('SCRIPT', cancel=cancel, forcar=True)
                self.assertFalse(outcome['ok'])
                self.assertEqual(old.read_bytes(), before_bytes)
                self.assertEqual(meta.read_bytes(), before_meta)

    def test_cancellation_after_staging_preserves_previous_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game, game_dir, _mod = self._game(root)
            (game_dir / 'saat_sfx.exe').touch()
            old = game.cache_dir() / 'export' / 'SCRIPT' / 'Bank_001' / 'sound_001.wav'
            old.parent.mkdir(parents=True)
            make_wav(old)
            meta = game._cache_meta_path('SCRIPT')
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(json.dumps({'pak': 'SCRIPT', 'fonte': game._assinatura_fonte('SCRIPT')}),
                            encoding='utf-8')
            before_bytes, before_meta = old.read_bytes(), meta.read_bytes()
            cancellation = threading.Event()
            FakeSaatClient.cancel_during_export = True
            with patch('studio.saat.audio.SaatClient', FakeSaatClient), patch.object(
                    game, 'saat', return_value={'sfx': game_dir / 'saat_sfx.exe', 'stream': None}):
                result = game.indexar('SCRIPT', cancel=cancellation, forcar=True)
            self.assertFalse(result['ok'])
            self.assertTrue(result['cancelado'])
            self.assertEqual(old.read_bytes(), before_bytes)
            self.assertEqual(meta.read_bytes(), before_meta)

    def test_metadata_write_failure_restores_previous_export_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game, game_dir, _mod = self._game(root)
            (game_dir / 'saat_sfx.exe').touch()
            old = game.cache_dir() / 'export' / 'SCRIPT' / 'Bank_001' / 'sound_001.wav'
            old.parent.mkdir(parents=True)
            make_wav(old)
            meta = game._cache_meta_path('SCRIPT')
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(json.dumps({'pak': 'SCRIPT', 'fonte': game._assinatura_fonte('SCRIPT'), 'prior': True}),
                            encoding='utf-8')
            before_bytes, before_meta = old.read_bytes(), meta.read_bytes()
            with patch('studio.saat.audio.SaatClient', FakeSaatClient), patch.object(
                    game, 'saat', return_value={'sfx': game_dir / 'saat_sfx.exe', 'stream': None}), \
                 patch('studio.saat.audio.json_atomico', side_effect=OSError('metadata write failed')):
                result = game.indexar('SCRIPT', forcar=True)
            self.assertFalse(result['ok'])
            self.assertEqual(old.read_bytes(), before_bytes)
            self.assertEqual(meta.read_bytes(), before_meta)

    def test_original_lookup_rejects_stale_requested_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game, game_dir, _mod = self._game(root)
            source = game.cache_dir() / 'export' / 'SPC_EA' / 'Bank_001' / 'sound_001.wav'
            source.parent.mkdir(parents=True)
            make_wav(source)
            meta = {'pak': 'SPC_EA', 'fonte': game._assinatura_fonte('SPC_EA')}
            game._cache_meta_path('SPC_EA').write_text(json.dumps(meta), encoding='utf-8')
            (game_dir / 'audio' / 'SFX' / 'SPC_EA').write_bytes(b'changed archive')
            game.carregar_indice()
            self.assertIsNone(game.original_para({'pak': 'SPC_EA', 'bank_num': 1, 'sound_num': 1}))

    def test_mod_loose_wavs_remain_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game, _game_dir, mod = self._game(root)
            (mod / 'audio' / 'SFX' / 'SCRIPT').unlink()
            loose = mod / 'audio' / 'SFX' / 'SCRIPT' / 'Bank_001' / 'sound_001.wav'
            loose.parent.mkdir(parents=True, exist_ok=True)
            make_wav(loose)
            result = game.indexar_mod('ModA', 'SPC_EA')
            self.assertTrue(result['ok'])
            self.assertEqual(result['motivo'], 'Mod já possui WAVs soltos')
            self.assertIn(('SCRIPT', 1, 1), game.indice_mod)

    def test_available_archives_lists_real_known_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game, _game_dir, _mod = self._game(root)
            self.assertEqual(game.available_archives('original'), ['SCRIPT', 'SPC_EA'])
            self.assertEqual(game.available_archives('mod'), ['SCRIPT'])


if __name__ == '__main__':
    unittest.main()
