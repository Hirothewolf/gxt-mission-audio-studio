import configparser
import tempfile
import unittest
import wave
import zipfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from studio.saat import audio as audio_module
from studio.saat.audio import GameAudio
from studio.saat.client import SaatClient
from studio.script.mapping import ref_do_registro


def write_wav(path, rate=8000, value=500):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate)
        wav.writeframes((value.to_bytes(2, 'little', signed=True)) * 80)


def make_ini(path):
    path.write_text(
        '[Archive]\nlookup_index = 3\nnum_banks = 207\n\n[Bank_207]\nnum_sounds = 7\n' +
        ''.join('sound_%03d.filename = Bank_207\\sound_%03d.wav\nsound_%03d.sample_rate = 8000\n' %
                (n, n, n) for n in range(1, 8)), encoding='utf-8')


class NativeModInstallerTests(unittest.TestCase):
    def test_packed_mod_rebuild_preserves_other_slots_and_uses_mod_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            game = tmp_path / 'game'; mod_name = 'TargetMod'
            game.mkdir()
            (game / 'gta_sa.exe').touch()
            (game / 'audio' / 'SFX').mkdir(parents=True)
            (game / 'audio' / 'SFX' / 'SCRIPT').write_bytes(b'base archive must not be used')
            (game / 'audio' / 'CONFIG').mkdir()
            (game / 'audio' / 'CONFIG' / 'BankLkup.dat').write_bytes(b'base lookup')
            mod_root = game / 'modloader' / mod_name
            (mod_root / 'text').mkdir(parents=True)
            gxt = mod_root / 'text' / 'american.gxt'; gxt.write_bytes(b'manual GXT fixture')
            gxt_before = gxt.read_bytes()
            archive = mod_root / 'audio' / 'SFX' / 'SCRIPT'
            lookup = mod_root / 'audio' / 'CONFIG' / 'BankLkup.dat'
            archive.parent.mkdir(parents=True); lookup.parent.mkdir(parents=True)
            ini_path = tmp_path / 'source.ini'; make_ini(ini_path)
            baseline = tmp_path / 'baseline'; baseline.mkdir()
            for n in range(1, 8):
                write_wav(baseline / 'Bank_207' / ('sound_%03d.wav' % n), value=100 + n)
            (baseline / 'sfx_import.ini').write_bytes(ini_path.read_bytes())
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zf:
                for path in baseline.rglob('*'):
                    if path.is_file():
                        zf.write(path, path.relative_to(baseline).as_posix())
            fixture_lookup = b''.join(bytes([3 if i < 218 else i % 3]) + bytes([i % 256]) * 11
                                      for i in range(710))
            lookup.write_bytes(fixture_lookup)
            replacement = tmp_path / 'replacement.wav'; write_wav(replacement, rate=16000, value=1400)
            ga = GameAudio({'game_dir': str(game), 'modloader_mod': mod_name, 'project_root': str(tmp_path)},
                           lambda: None, tmp_path / 'cache', tmp_path / 'backups',
                           tmp_path / 'workspace' / 'modloader_manifest.json')
            ga.saat = lambda: {'sfx': tmp_path / 'saat_sfx.exe', 'stream': None}
            ga._game_is_running = lambda: False
            temp_files = {'archive': None, 'lookup': None}

            def fake_export(self, pak, output, source=None, bank=None, **kwargs):
                self_test.assertEqual(Path(source), temp_files['archive'])
                self_test.assertEqual(Path(bank), temp_files['lookup'])
                with zipfile.ZipFile(source) as zf:
                    zf.extractall(Path(output) / pak)
                return {'ok': True, 'rc': 0, 'wavs': 7}

            def export_with_capture(client, pak, output, source=None, bank=None, **kwargs):
                temp_files['archive'], temp_files['lookup'] = Path(source), Path(bank)
                return fake_export(client, pak, output, source, bank, **kwargs)

            def fake_import(args, cwd, label):
                native, recipe, imported_lookup = map(Path, args[1:4])
                self_test.assertEqual(imported_lookup, temp_files['lookup'])
                ini = configparser.ConfigParser(interpolation=None); ini.optionxform = str
                ini.read(recipe, encoding='utf-8-sig')
                with zipfile.ZipFile(native, 'w', zipfile.ZIP_DEFLATED) as zf:
                    zf.write(recipe, 'sfx_import.ini')
                    for section in ini.sections():
                        if not section.startswith('Bank_'):
                            continue
                        for n in range(1, ini.getint(section, 'num_sounds') + 1):
                            name = ini.get(section, 'sound_%03d.filename' % n)
                            zf.write(name, 'Bank_207/sound_%03d.wav' % n)

            self_test = self
            with patch.object(SaatClient, 'exportar_sfx', export_with_capture), patch.object(ga, '_run_native_saat', fake_import):
                plan = ga.planejar([{'rotulo': 'fixture', 'ref': ('SCRIPT', 207, 7), 'dub': str(replacement)}], mod_name)
                self.assertEqual(plan[0]['acao'], 'reempacotar_nativo')
                self.assertEqual(plan[0]['destino'], archive)
                result = ga.instalar(plan, mod_name)
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['instalados'], 1)
            self.assertEqual(lookup.read_bytes(), fixture_lookup)
            self.assertEqual(result['arquivos_nativos'][0]['archive'], str(archive.resolve()))
            proof = result['arquivos_nativos'][0]['slots_verified']
            self.assertEqual(len(proof), 1)
            self.assertEqual(proof[0]['ref'], ['SCRIPT', 207, 7])
            self.assertEqual(proof[0]['source_take_sha256'], audio_module.sha256_arquivo(replacement))
            audition = Path(proof[0]['audition_path'])
            self.assertTrue(audition.is_file())
            self.assertEqual(proof[0]['audition_sha256'], audio_module.sha256_arquivo(audition))
            self.assertTrue(Path(result['backup'], 'audio', 'SFX', 'SCRIPT').is_file())
            self.assertEqual(gxt.read_bytes(), gxt_before)
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(tmp_path / 'after')
            after = tmp_path / 'after'
            for n in range(1, 7):
                self.assertEqual(GameAudio._pcm_signature(after / 'Bank_207' / ('sound_%03d.wav' % n)),
                                 GameAudio._pcm_signature(baseline / 'Bank_207' / ('sound_%03d.wav' % n)))
            self.assertNotEqual(GameAudio._pcm_signature(after / 'Bank_207' / 'sound_007.wav'),
                                GameAudio._pcm_signature(baseline / 'Bank_207' / 'sound_007.wav'))
            installed_archive, installed_lookup = archive.read_bytes(), lookup.read_bytes()
            real_replace = audio_module.os.replace
            def fail_bank_restore(source, target):
                if Path(target) == lookup and str(source).endswith('.studio-restore'):
                    raise OSError('injected second-file restore failure')
                return real_replace(source, target)
            with patch.object(audio_module.os, 'replace', side_effect=fail_bank_restore):
                failed_restore = ga.desinstalar(mod_name)
            self.assertFalse(failed_restore['ok'])
            self.assertEqual(archive.read_bytes(), installed_archive)
            self.assertEqual(lookup.read_bytes(), installed_lookup)
            restored = ga.desinstalar(mod_name)
            self.assertTrue(restored['ok'], restored)
            self.assertEqual(restored['restaurados'], 1)
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(tmp_path / 'restored')
            self.assertEqual(GameAudio._pcm_signature(tmp_path / 'restored' / 'Bank_207' / 'sound_007.wav'),
                             GameAudio._pcm_signature(baseline / 'Bank_207' / 'sound_007.wav'))
            archive_before, lookup_before = archive.read_bytes(), lookup.read_bytes()
            def fail_bank_publish(source, target):
                if Path(target) == lookup and str(source).endswith('.studio-new'):
                    raise OSError('injected second-file publish failure')
                return real_replace(source, target)
            with patch.object(SaatClient, 'exportar_sfx', export_with_capture), \
                 patch.object(ga, '_run_native_saat', fake_import), \
                 patch.object(audio_module.os, 'replace', side_effect=fail_bank_publish):
                failed = ga.instalar(plan, mod_name)
            self.assertFalse(failed['ok'])
            self.assertEqual(archive.read_bytes(), archive_before)
            self.assertEqual(lookup.read_bytes(), lookup_before)
            self.assertEqual(gxt.read_bytes(), gxt_before)

    def test_streams_do_not_resolve_to_sfx_slots(self):
        self.assertIsNone(ref_do_registro({'audio_kind': 'CUTSCENE', 'stream': 'CUTSCENE', 'track': 2}))
        self.assertIsNone(ref_do_registro({'audio_kind': 'BEATS', 'stream': 'BEATS', 'track': 2}))
        self.assertEqual(ref_do_registro({'bank_num': 207, 'sound_num': 7}), ('SCRIPT', 207, 7))
        self.assertEqual(ref_do_registro({'bank_index': 206, 'sound_index': 6}), ('SCRIPT', 207, 7))
        self.assertIsNone(ref_do_registro({'stable_audio_id': 'CUTSCENE:INTRO1:PRO@24',
                                           'bank_num': 207, 'sound_num': 7}))
        result = GameAudio._instalar_native(object.__new__(GameAudio),
            [{'ref': ('CUTSCENE', 1, 2), 'dub': 'unused'}], 'TargetMod')
        self.assertFalse(result['ok'])
        self.assertIn('streams', result['motivo'])

    def test_native_write_guards_fail_closed_on_bad_mod_path_query_or_duplicate_slot(self):
        with tempfile.TemporaryDirectory() as temporary:
            modloader = Path(temporary) / 'modloader'
            (modloader / 'FixtureMod').mkdir(parents=True)
            ga = object.__new__(GameAudio)
            ga.modloader_dir = lambda: modloader
            self.assertEqual(ga._safe_mod_dir('FixtureMod'), (modloader / 'FixtureMod').resolve())
            for invalid in ('..', '../outside', 'FixtureMod/child', 'C:\\outside'):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    ga._safe_mod_dir(invalid)

        with patch.object(audio_module.subprocess, 'run', return_value=SimpleNamespace(
                returncode=1, stdout=b'', stderr=b'PowerShell unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'Não foi possível confirmar'):
                ga._game_is_running()
        duplicate = {'ref': ('SCRIPT', 1, 1), 'dub': 'unused'}
        rejected = GameAudio._instalar_native(object.__new__(GameAudio), [duplicate, duplicate], 'FixtureMod')
        self.assertFalse(rejected['ok'])
        self.assertIn('mais de um take', rejected['motivo'])


if __name__ == '__main__':
    unittest.main()
