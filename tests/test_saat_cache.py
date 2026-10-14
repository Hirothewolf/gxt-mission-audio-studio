import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from studio.saat.audio import GameAudio
from test_review import make_wav


class SaatCacheTests(unittest.TestCase):
    def _make_game(self, root, mod_name='ModA'):
        game_dir = root / 'game'
        base_sfx = game_dir / 'audio' / 'SFX'
        base_cfg = game_dir / 'audio' / 'CONFIG'
        base_sfx.mkdir(parents=True)
        base_cfg.mkdir(parents=True)
        (base_sfx / 'SCRIPT').write_bytes(b'base-script')
        (base_cfg / 'BankLkup.dat').write_bytes(b'base-bank')
        mod_dir = game_dir / 'modloader' / mod_name
        mod_sfx = mod_dir / 'audio' / 'SFX'
        mod_cfg = mod_dir / 'audio' / 'CONFIG'
        mod_sfx.mkdir(parents=True)
        mod_cfg.mkdir(parents=True)
        (mod_sfx / 'SCRIPT').write_bytes(b'mod-script')
        return game_dir, mod_dir, {
            'ok': True, 'pasta': game_dir, 'script': base_sfx / 'SCRIPT',
            'banklkup': base_cfg / 'BankLkup.dat', 'modloader': game_dir / 'modloader',
            'faltando': [],
        }

    def test_invalid_game_cache_status_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = GameAudio({}, lambda: None, root / 'cache', root / 'backup', root / 'manifest.json')
            status = game.estado_cache()
            self.assertFalse(status['indexado'])
            self.assertEqual(status['wavs_mod'], 0)

    def test_reads_cache_metadata_and_detects_changed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = GameAudio({}, lambda: None, root / 'cache', root / 'backup', root / 'manifest.json')
            game.indice_mod = {}
            directory = game.cache_dir()
            directory.mkdir(parents=True)
            (directory / 'cache_meta.json').write_text(json.dumps({'fonte': {'SCRIPT': ['old']}}))
            self.assertTrue(game.estado_cache()['desatualizado'])

    def test_project_originals_do_not_count_as_installed_mod(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'extracted' / 'audio' / 'sfx' / 'SCRIPT' / 'Bank_001' / 'sound_001.wav'
            path.parent.mkdir(parents=True)
            make_wav(path)
            info = {'ok': True, 'pasta': root, 'modloader': root / 'modloader'}
            with patch('studio.saat.audio.PROJ_ROOT', root), patch('studio.saat.audio.validar_pasta_jogo', return_value=info):
                game = GameAudio({}, lambda: None, root / 'cache', root / 'backup', root / 'manifest.json')
            self.assertEqual(game.indice_mod, {})

    def test_generic_project_original_is_not_returned_as_installed_mod(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = {'ok': True, 'pasta': root, 'modloader': root / 'modloader', 'faltando': []}
            path = root / 'extracted' / 'audio' / 'sfx' / 'SCRIPT' / 'Bank_001' / 'sound_001.wav'
            path.parent.mkdir(parents=True)
            make_wav(path)
            with patch('studio.saat.audio.PROJ_ROOT', root), \
                 patch('studio.saat.audio.validar_pasta_jogo', return_value=info):
                game = GameAudio({}, lambda: None, root / 'cache', root / 'backup', root / 'manifest.json')
                game._ml_escaneado = True
                self.assertIsNone(game.instalado_para({'pak': 'SCRIPT', 'bank_num': 1, 'sound_num': 1}))

    def test_stale_original_cache_is_not_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game_dir, _mod_dir, info = self._make_game(root)
            cache_root = root / 'cache'
            game = None
            with patch('studio.saat.audio.validar_pasta_jogo', return_value=info):
                game = GameAudio({'game_dir': str(game_dir)}, lambda: None, cache_root,
                                 root / 'backup', root / 'manifest.json')
            source = game.cache_dir() / 'export' / 'SCRIPT' / 'Bank_001' / 'sound_001.wav'
            source.parent.mkdir(parents=True)
            make_wav(source)
            (game.cache_dir() / 'cache_meta.json').write_text(
                json.dumps({'fonte': {'SCRIPT': ['stale']}}), encoding='utf-8'
            )
            game.carregar_indice()
            self.assertTrue(game.estado_cache()['desatualizado'])
            self.assertIsNone(game.original_para({'pak': 'SCRIPT', 'bank_num': 1, 'sound_num': 1}))

    def test_stale_mod_export_cache_is_ignored_but_direct_mod_wav_remains_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game_dir, mod_dir, info = self._make_game(root)
            cfg = {'game_dir': str(game_dir), 'modloader_mod': 'ModA'}
            cache_root = root / 'cache'
            mod_slug = hashlib.sha1(b'moda').hexdigest()[:10]
            exported = root / 'saat_mod' / mod_slug / 'export' / 'SCRIPT' / 'Bank_001' / 'sound_001.wav'
            exported.parent.mkdir(parents=True)
            make_wav(exported)
            meta = exported.parents[3] / 'cache_meta.json'
            meta.write_text(json.dumps({'mod': 'ModA', 'pak': 'SCRIPT', 'fonte': {'SCRIPT': ['old']}}),
                            encoding='utf-8')
            with patch('studio.saat.audio.validar_pasta_jogo', return_value=info):
                game = GameAudio(cfg, lambda: None, cache_root, root / 'backup', root / 'manifest.json')
            self.assertEqual(game.indice_mod, {})

            direct = mod_dir / 'audio' / 'SFX' / 'SCRIPT' / 'Bank_001' / 'sound_001.wav'
            (mod_dir / 'audio' / 'SFX' / 'SCRIPT').unlink()
            direct.parent.mkdir(parents=True)
            make_wav(direct)
            game._ml_escaneado = False
            self.assertEqual(game.instalado_para({'pak': 'SCRIPT', 'bank_num': 1, 'sound_num': 1}), direct)

    def test_active_mod_missing_override_does_not_return_another_mods_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game_dir, _mod_a_dir, info = self._make_game(root, mod_name='ModA')
            mod_b = game_dir / 'modloader' / 'ModB' / 'audio' / 'SFX' / 'SCRIPT'
            other_mod_wav = mod_b / 'Bank_001' / 'sound_001.wav'
            other_mod_wav.parent.mkdir(parents=True)
            make_wav(other_mod_wav)
            cfg = {'game_dir': str(game_dir), 'modloader_mod': 'ModA'}

            with patch('studio.saat.audio.validar_pasta_jogo', return_value=info):
                game = GameAudio(cfg, lambda: None, root / 'cache', root / 'backup', root / 'manifest.json')

            self.assertIsNone(game.instalado_para({'pak': 'SCRIPT', 'bank_num': 1, 'sound_num': 1}))

    def test_requested_mod_uses_only_its_fresh_pak_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game_dir, _mod_a_dir, info = self._make_game(root, mod_name='ModA')
            mod_b_sfx = game_dir / 'modloader' / 'ModB' / 'audio' / 'SFX' / 'SCRIPT'
            mod_b_sfx.parent.mkdir(parents=True)
            (mod_b_sfx.parent / 'SCRIPT').write_bytes(b'mod-b-script')
            cfg = {'game_dir': str(game_dir), 'modloader_mod': 'ModA'}

            with patch('studio.saat.audio.validar_pasta_jogo', return_value=info):
                game = GameAudio(cfg, lambda: None, root / 'cache', root / 'backup', root / 'manifest.json')

            ref = {'pak': 'SCRIPT', 'bank_num': 1, 'sound_num': 1}
            active_cache = game.cache_dir_mod('ModA')
            active_wav = active_cache / 'export' / 'SCRIPT' / 'Bank_001' / 'sound_001.wav'
            active_wav.parent.mkdir(parents=True)
            make_wav(active_wav)
            (active_cache / 'cache_meta_SCRIPT.json').write_text(json.dumps({
                'mod': 'ModA', 'pak': 'SCRIPT', 'fonte': game._assinatura_fonte_mod('ModA', 'SCRIPT'),
            }), encoding='utf-8')
            game.carregar_indice(mod_nome='ModA')

            requested_cache = game.cache_dir_mod('ModB')
            requested_wav = requested_cache / 'export' / 'SCRIPT' / 'Bank_001' / 'sound_001.wav'
            requested_wav.parent.mkdir(parents=True)
            make_wav(requested_wav)
            requested_meta = requested_cache / 'cache_meta_SCRIPT.json'
            requested_meta.write_text(json.dumps({
                'mod': 'ModB', 'pak': 'SCRIPT', 'fonte': {'SCRIPT': ['stale']},
            }), encoding='utf-8')

            self.assertIsNone(game.instalado_para(ref, mod='ModB'))

            requested_meta.write_text(json.dumps({
                'mod': 'ModB', 'pak': 'SCRIPT', 'fonte': game._assinatura_fonte_mod('ModB', 'SCRIPT'),
            }), encoding='utf-8')
            self.assertEqual(game.instalado_para(ref, mod='ModB'), requested_wav)


if __name__ == '__main__':
    unittest.main()
