import unittest
from studio.tts.timing import resolve_duration, initial_timing_settings

class TimingTests(unittest.TestCase):
    def test_original_is_per_utterance_not_global(self):
        self.assertEqual(resolve_duration('original',2.47),2.4)
        self.assertEqual(resolve_duration('original',5.86),5.8)
        self.assertEqual(resolve_duration('auto',.08),.08)
        self.assertEqual(resolve_duration('original',2.0),2.0)
    def test_free_and_manual_remain_explicit(self):
        self.assertIsNone(resolve_duration('livre',2.47))
        self.assertIsNone(resolve_duration(None,2.47))
        self.assertEqual(resolve_duration('3,7',2.47),3.7)
    def test_missing_invalid_original_never_defaults_two_seconds(self):
        for value in (None,0,-1,float('inf'),float('nan')):
            with self.subTest(value=value),self.assertRaises(ValueError):resolve_duration('original',value)
    def test_legacy_fixed_preferences_migrate_once(self):
        self.assertEqual(initial_timing_settings({'omnivoice_speed':'1.15','omnivoice_duration':'2'}),(1.0,'livre'))
        self.assertEqual(initial_timing_settings({'omnivoice_timing_policy_version':2,'omnivoice_speed':'.9','omnivoice_duration':'livre'}),('.9','livre'))
    def test_fresh_settings_and_previous_default_allow_full_speech(self):
        self.assertEqual(initial_timing_settings({}),(1.0,'livre'))
        self.assertEqual(initial_timing_settings({'omnivoice_timing_policy_version':2,'omnivoice_duration':'original'}),(1.0,'livre'))
        self.assertEqual(initial_timing_settings({'omnivoice_timing_policy_version':3,'omnivoice_duration':'original'}),(1.0,'original'))
        self.assertEqual(initial_timing_settings({'omnivoice_timing_policy_version':2,'omnivoice_duration':'13.5'}),(1.0,'13.5'))

class EditorOriginalTimingTests(unittest.TestCase):
    def test_request_uses_target_native_wav_even_with_alternate_cj_voice(self):
        import tempfile,wave,json,hashlib
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from studio.ui import app as module
        from studio.tts import omnivoice
        for manual_cj in (False,True):
            with self.subTest(manual_cj=manual_cj),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);target=root/'target.wav';other=root/'other.wav'
                for path,seconds in [(target,2.47),(other,10)]:
                    with wave.open(str(path),'wb') as f:
                        f.setparams((1,2,1000,0,'NONE','not compressed'));f.writeframes(b'\x01\x00'*int(seconds*1000))
                sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
                base=root/'base.gxt';base.write_bytes(b'original GXT proof')
                runtime=root/'python.exe';runtime.write_bytes(b'runtime')
                worker=root/'worker.py';worker.write_bytes(b'worker')
                engine=SimpleNamespace(python=runtime,script=worker)
                a=module.App.__new__(module.App);a.jogo=object()
                a.var_omnivoice_cj_reference=SimpleNamespace(get=lambda:manual_cj)
                a.var_omnivoice_speed=SimpleNamespace(get=lambda:'1.0')
                a.var_omnivoice_duration=SimpleNamespace(get=lambda:'original')
                a.info_script=lambda *args:{'pak':'SCRIPT','bank_index':0,'sound_index':0,'stable_audio_id':'SCRIPT:B000:S000',
                    'duration_seconds':99,'speaker_identity':'CJ','speaker_identity_status':'MANUAL'}
                native={'path':str(target),'sha256':sha(target),'source_kind':'original_game_saat_cache','duration_seconds':99}
                clone={'path':str(other),'sha256':sha(other),'reference_text':'Alternate CJ words.',
                    'reference_kind':'accepted_original_cutscene_utterance','duration_seconds':10,
                    'provenance':{'reference_source_path':str(other),'reference_source_sha256':sha(other),'utterance_id':'CJ_REF'}}
                with patch.object(module,'PROJ_ROOT',root),patch('studio.audio.library_context.base_gxt_path',return_value=base), \
                     patch('studio.audio.library_context.base_texts',return_value={('MAIN',1):'Native target words.'}), \
                     patch.object(omnivoice,'resolve_original_reference',return_value=native), \
                     patch.object(omnivoice,'load_accepted_cj_reference',return_value=clone):
                    command=a._omnivoice_command_for_selection(engine,root/'run','MAIN',1,'EVENT','Texto PT.',announce=False)
                request=json.loads(Path(command[-1]).read_text())
                self.assertEqual(request['window_seconds'],2.47)
                self.assertEqual(request['duration'],2.4)
                self.assertEqual(request['speed'],1.0)
                self.assertEqual(request['reference_path'],str(other if manual_cj else target))

if __name__=='__main__':unittest.main()
