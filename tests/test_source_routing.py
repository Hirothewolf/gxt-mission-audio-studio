import unittest
from pathlib import Path
from studio.audio.source_routing import audio_route, output_directory
class RoutingTests(unittest.TestCase):
 def test_script_keeps_native_coordinates(self):
  route=audio_route({'stable_audio_id':'SCRIPT:B206:S006','bank_num':207,'sound_num':7})
  self.assertEqual(route['family'],'SFX');self.assertEqual(route['container'],'SCRIPT')
  self.assertEqual(route['destination'],'audio/SFX/SCRIPT/Bank_207/sound_007.wav')
 def test_police_is_beats_not_script(self):
  route=audio_route({'stable_audio_id':'POLICE:PRO@24'})
  self.assertEqual(route['destination'],'audio/streams/BEATS/Track_010.ogg')
  self.assertEqual(output_directory(Path('run'),route,'INTRO1'),Path('run/output/streams/BEATS/Track_010/INTRO1'))
 def test_cutscene_track_preserved(self):
  route=audio_route({'pak':'CUTSCENE','stream':{'track':'Track_078'}})
  self.assertEqual(route['container'],'CUTSCENE')
 def test_ambiguous_and_unsafe_stream_rejected(self):
  with self.assertRaises(ValueError):audio_route({'pak':'CUTSCENE','stream':{'track':'../../oops'}})
  with self.assertRaises(ValueError):audio_route({'pak':'CUTSCENE','stream':{'track':'Track_001'},'cutscene_ambiguous':True})
 def test_conflicting_stream_and_bank_metadata_is_rejected(self):
  with self.assertRaises(ValueError):audio_route({'stable_audio_id':'POLICE:PRO@24','bank_num':1,'sound_num':1})
 def test_routed_request_is_the_import_authority(self):
  import json,tempfile
  from studio.audio.source_routing import command_output_directory
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);output=root/'output/streams/BEATS/Track_010/INTRO1/take.wav'
   request=root/'request.json';request.write_text(json.dumps({'output_path':str(output)}))
   self.assertEqual(command_output_directory(['python','--request',str(request)]),output.parent.resolve())
 def test_no_script_default_for_unknown(self):
  self.assertIsNone(audio_route({'stable_audio_id':'UNKNOWN:hello'}))
 def test_cutscene_identity_cannot_fall_back_to_script_without_pak(self):
  with self.assertRaises(ValueError):audio_route({'stable_audio_id':'CUTSCENE:INTRO1:PRO@01','bank_num':1,'sound_num':1})
 def test_explicit_stream_route_is_detected_by_all_consumers(self):
  from studio.audio.source_routing import is_stream_record
  record={'audio_route':{'family':'streams','container':'BEATS','track':'Track_010'}}
  self.assertTrue(is_stream_record(record))
  self.assertEqual(audio_route(record)['container'],'BEATS')
if __name__=='__main__':unittest.main()
