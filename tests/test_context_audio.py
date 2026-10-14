import hashlib
import json
import struct
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import patch


def write_wav(path, samples=(100, -100, 200, -200), rate=8000):
    with wave.open(str(path), 'wb') as out:
        out.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        out.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
    return path


def data_sha256(path):
    raw = Path(path).read_bytes()
    offset = raw.index(b'data') + 8
    return hashlib.sha256(raw[offset:]).hexdigest()


class FakeRoot:
    def __init__(self):
        self.jobs = {}
        self.next_id = 0

    def after(self, delay_ms, callback):
        self.next_id += 1
        self.jobs[self.next_id] = (delay_ms, callback)
        return self.next_id

    def after_cancel(self, job):
        self.jobs.pop(job, None)

    def run_next(self):
        job = next(iter(self.jobs))
        _delay, callback = self.jobs.pop(job)
        callback()


class ContextAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_project_root_change_is_live_for_mapping_and_wav_resolution(self):
        import studio.config as config
        import studio.script.mapping as mapping
        import studio.audio.wav as wav

        old_root = config.PROJ_ROOT
        new_root = self.root / 'new-project'
        (new_root / 'manifests').mkdir(parents=True)
        manifest = new_root / 'manifests' / 'script_mapping_v3.jsonl'
        manifest.write_text(json.dumps({
            'gxt_stable_id': 'TEST:12345678', 'stable_audio_id': 'SCRIPT:B000:S000'
        }) + '\n', encoding='utf-8')
        clip = new_root / 'clip.wav'
        clip.parent.mkdir(parents=True, exist_ok=True)
        write_wav(clip)
        old_cfg_root = config.CFG.get('project_root')
        try:
            with patch.object(config, 'cfg_save'):
                config.set_projeto_root(new_root)
            self.assertEqual(config.get_project_root(), new_root.resolve())
            self.assertIn(('TEST', 0x12345678), mapping.carregar_script_cache())
            self.assertEqual(wav.resolver_wav('clip.wav'), clip)
        finally:
            with patch.object(config, 'cfg_save'):
                config.set_projeto_root(old_root)
            if old_cfg_root is None:
                config.CFG.pop('project_root', None)
            else:
                config.CFG['project_root'] = old_cfg_root
            mapping._SCRIPT_CACHE = None
            wav._WAV_RES.clear()

    def test_non_script_stable_audio_id_maps_to_one_based_saat_path(self):
        from studio.script.mapping import ref_do_registro

        self.assertEqual(
            ref_do_registro({'archive': 'SPC_GA', 'stable_audio_id': 'SPC_GA:B000:S000'}),
            ('SPC_GA', 1, 1),
        )
        self.assertEqual(
            ref_do_registro({'stable_audio_id': 'SPC_GA:B000:S000', 'bank_num': 1, 'sound_num': 1}),
            ('SPC_GA', 1, 1),
        )

    def test_resolver_uses_pak_and_rejects_unqualified_collision(self):
        from studio.audio.resolver import BaseAudioResolver

        ga = self.root / 'SPC_GA' / 'Bank_001' / 'sound_001.wav'
        pa = self.root / 'SPC_PA' / 'Bank_001' / 'sound_001.wav'
        ga.parent.mkdir(parents=True)
        pa.parent.mkdir(parents=True)
        write_wav(ga, (100, 200, 300, 400))
        write_wav(pa, (-100, -200, -300, -400))
        resolver = BaseAudioResolver()
        resolver.scan([self.root])

        chosen, _how, confidence = resolver.resolve(
            'TAB', 1, ainfo={'archive': 'SPC_GA', 'bank_num': 1, 'sound_num': 1}
        )
        self.assertEqual(chosen, ga)
        self.assertGreater(confidence, 0)

        pak_root_resolver = BaseAudioResolver()
        pak_root_resolver.scan([ga.parents[1]])
        chosen, _how, _confidence = pak_root_resolver.resolve(
            'TAB', 1, ainfo={'archive': 'SPC_GA', 'bank_num': 1, 'sound_num': 1}
        )
        self.assertEqual(chosen, ga)

        chosen, _how, confidence = resolver.resolve(
            'TAB', 1, ainfo={'bank_num': 1, 'sound_num': 1}
        )
        self.assertIsNone(chosen)
        self.assertEqual(confidence, 0)

    def test_manifest_pcm_hash_mismatch_is_not_trusted(self):
        from studio.audio.resolver import BaseAudioResolver

        clip = write_wav(self.root / 'source.wav')
        resolver = BaseAudioResolver()
        resolver.scan([self.root])
        found, _how, confidence = resolver.resolve(
            'TAB', 1,
            ainfo={'source_wav_path': str(clip), 'pcm_sha256': '0' * 64},
        )
        self.assertIsNone(found)
        self.assertEqual(confidence, 0)

    def test_wav_info_rejects_truncated_data_chunk(self):
        from studio.audio.wav import wav_info

        clip = write_wav(self.root / 'valid.wav')
        raw = bytearray(clip.read_bytes())
        data_at = raw.index(b'data')
        declared = struct.unpack_from('<I', raw, data_at + 4)[0]
        struct.pack_into('<I', raw, data_at + 4, declared + 8)
        bad = self.root / 'truncated.wav'
        bad.write_bytes(raw)
        self.assertIsNotNone(wav_info(clip))
        self.assertIsNone(wav_info(bad))

    def test_float_wav_metadata_and_conversion_remain_supported(self):
        from studio.audio.wav import converter_wav_pcm16_mono, wav_info

        samples = struct.pack('<ff', 0.25, -0.5)
        fmt = struct.pack('<HHIIHH', 3, 1, 8000, 32000, 4, 32)
        body = (b'fmt ' + struct.pack('<I', len(fmt)) + fmt +
                b'data' + struct.pack('<I', len(samples)) + samples)
        raw = b'RIFF' + struct.pack('<I', len(body) + 4) + b'WAVE' + body
        clip = self.root / 'float.wav'
        clip.write_bytes(raw)

        info = wav_info(clip)
        converted = converter_wav_pcm16_mono(clip)
        self.assertEqual((info['fmt'], info['bits'], info['dur']), (3, 32, 2 / 8000))
        self.assertEqual(wav_info(self._write_bytes('converted.wav', converted))['fmt'], 1)

    def _write_bytes(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_diagnostic_skips_saat_when_game_audio_is_none(self):
        import studio.config as config
        import studio.tts.downloader as downloader
        import studio.ui.diagnostic as diagnostic

        class Widget:
            def __init__(self, *_args, **_kwargs):
                pass

            def __getattr__(self, _name):
                return lambda *_args, **_kwargs: None

        class App:
            g = None
            c = None
            script_db = {}
            jogo = None
            root = Widget()
            ws = type('Workspace', (), {'path': str(self.root / 'review.json')})()

            @staticmethod
            def _copiar(*_args):
                pass

        old_root = config.PROJ_ROOT
        try:
            config.PROJ_ROOT = self.root
            with patch.object(diagnostic.tk, 'Toplevel', Widget), \
                 patch.object(diagnostic.tk, 'Text', Widget), \
                 patch.object(diagnostic.ttk, 'Frame', Widget), \
                 patch.object(diagnostic.ttk, 'Button', Widget), \
                 patch.object(diagnostic, 'detectar_engines', return_value=[]), \
                 patch.object(downloader, 'listar_modelos', return_value=[]):
                diagnostic.abrir_diagnostico(App())
        finally:
            config.PROJ_ROOT = old_root

    def test_player_cancels_previous_sequence_and_honors_zero_pause(self):
        import studio.audio.player as player_module

        class FakeWinSound:
            SND_FILENAME = 1
            SND_ASYNC = 2
            calls = []

            @classmethod
            def PlaySound(cls, path, flags):
                cls.calls.append((path, flags))

        root = FakeRoot()
        player = player_module.Player(root)
        a = write_wav(self.root / 'a.wav')
        b = write_wav(self.root / 'b.wav')
        steps = []
        done = []
        original = player_module.winsound
        try:
            player_module.winsound = FakeWinSound
            player.play_sequence([a, b], pausa_ms=0, on_step=lambda i, _p: steps.append(i),
                                 on_done=lambda: done.append(threading.get_ident()))
            self.assertEqual(steps, [0])
            self.assertEqual(next(iter(root.jobs.values()))[0], 0)
            root.run_next()
            self.assertEqual(steps, [0])
            self.assertEqual(len(root.jobs), 1)
            self.assertEqual(next(iter(root.jobs.values()))[0], 0)
            root.run_next()
            self.assertEqual(steps, [0, 1])
            player.play_sequence([b], pausa_ms=0, on_step=lambda i, _p: steps.append(10 + i),
                                 on_done=lambda: done.append(threading.get_ident()))
            self.assertEqual(len(root.jobs), 1)
            self.assertEqual(steps, [0, 1, 10])
            root.run_next()
            self.assertEqual(done, [threading.get_ident()])
            self.assertEqual(FakeWinSound.calls[-1][1], FakeWinSound.SND_FILENAME | FakeWinSound.SND_ASYNC)
        finally:
            player_module.winsound = original


if __name__ == '__main__':
    unittest.main()
