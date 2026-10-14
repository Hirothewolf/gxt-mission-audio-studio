import hashlib,json,tempfile,unittest
from pathlib import Path
import numpy as np
import soundfile as sf
from studio.tts.stream_reference import resolve_police_reference
from studio.tts.omnivoice import sha256
class PoliceReferenceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.folder=self.root/'reports/studio_rework_20261008/omnivoice_cast_utterance_references_v1'
  self.folder.mkdir(parents=True);(self.folder/'reference_asr').mkdir()
  self.source=self.root/'BEATS_010.wav';self.clip=self.root/'clip.wav'
  values=np.arange(32000,dtype=np.int16)
  sf.write(self.source,values,16000,subtype='PCM_16');sf.write(self.clip,values[:16000],16000,subtype='PCM_16')
  self.receipt={'id':'POLICE:PRO@24','gxt_id':'INTRO1:7F49AF6F','reference_relation':'SAME_ORIGINAL_UTTERANCE','reference_source':str(self.source),'reference_source_sha256':sha256(self.source),'reference_path':str(self.clip),'reference_sha256':sha256(self.clip),'reference_start_seconds':0,'reference_end_seconds':1,'native_transcript':'How you been, Carl?'}
  self.receipt_path=self.folder/'POLICE_PRO_24_canary_receipt.json';self.receipt_path.write_text(json.dumps(self.receipt))
  self.proof={'audio_path':str(self.clip),'audio_sha256':sha256(self.clip),'blind_no_target_prompt':True,'transcript':'How you been, Carl?','wer':0}
  self.proof_path=self.folder/'reference_asr/POLICE_PRO_24.json';self.proof_path.write_text(json.dumps(self.proof))
  self.record={'stable_audio_id':'POLICE:PRO@24','gxt_stable_id':'INTRO1:7F49AF6F'}
 def tearDown(self):self.tmp.cleanup()
 def test_resolves_exact_original_segment_without_script_coordinates(self):
  ref=resolve_police_reference(self.record,self.root)
  self.assertEqual(ref['path'],str(self.clip.resolve()))
  self.assertEqual(ref['duration_seconds'],1)
  self.assertEqual(ref['reference_text'],'How you been, Carl?')
  self.assertEqual(ref['provenance']['reference_end_frame'],16000)
 def test_rejects_other_line_identity(self):
  self.record['gxt_stable_id']='INTRO1:00000001'
  with self.assertRaises(ValueError):resolve_police_reference(self.record,self.root)
 def test_rejects_changed_source_or_clip(self):
  self.clip.write_bytes(b'changed')
  with self.assertRaises(ValueError):resolve_police_reference(self.record,self.root)
 def test_rejects_unproven_transcript(self):
  self.proof['blind_no_target_prompt']=False;self.proof_path.write_text(json.dumps(self.proof))
  with self.assertRaises(ValueError):resolve_police_reference(self.record,self.root)
if __name__=='__main__':unittest.main()
