import unittest
from studio.audio.stream_catalog import align_dialogue, annotate_overlaps
class StreamAlignmentTests(unittest.TestCase):
 def test_overlapping_windows_retain_both_original_intervals(self):
  rows=[{'gid':'INTRO1:11111111','stable_audio_id':'POLICE:a','container':'BEATS','track':'Track_010','source_sha256':'same','start':1.,'end':4.,'status':'ALIGNMENT_CANDIDATE'},
        {'gid':'INTRO1:22222222','stable_audio_id':'POLICE:b','container':'BEATS','track':'Track_010','source_sha256':'same','start':3.,'end':5.,'status':'ALIGNMENT_CANDIDATE'}]
  annotate_overlaps(rows)
  self.assertEqual([(r['start'],r['end']) for r in rows],[(1.,4.),(3.,5.)])
  self.assertEqual([r['status'] for r in rows],['ALIGNMENT_CANDIDATE']*2)
  self.assertEqual(rows[0]['overlapping_audio_ids'],['POLICE:b'])
  self.assertFalse(rows[0]['overlap_voice_identity_verified'])
 def test_repeated_lines_stay_in_timeline_order(self):
  rows=[{'id':'a','native_caption_en':'Yes Carl'},{'id':'b','native_caption_en':'No Carl'},{'id':'c','native_caption_en':'Yes Carl'}]
  words=[{'word':w,'start':i,'end':i+.5} for i,w in enumerate('Yes Carl No Carl Yes Carl'.split())]
  result=align_dialogue(rows,words)
  self.assertEqual([(x['start'],x['end']) for x in result],[(0,1.5),(2,3.5),(4,5.5)])
 def test_partial_reference_does_not_steal_the_following_speaker(self):
  rows=[{'id':'radio','native_caption_en':'This is car 58 WHAT'},{'id':'cop','native_caption_en':'See you around Carl'}]
  words=[{'word':w,'start':i,'end':i+.5} for i,w in enumerate('This is car 58 See you around Carl'.split())]
  result=align_dialogue(rows,words)
  self.assertTrue(result[0]['partial']);self.assertEqual(result[0]['end'],3.5)
  self.assertEqual(result[1]['start'],4)
 def test_missing_line_is_not_assigned_another_voice(self):
  result=align_dialogue([{'id':'a','native_caption_en':'Entirely absent dialogue'}],[{'word':'hello','start':0,'end':1}])
  self.assertFalse(result[0]['usable'])
class DecodedStreamReferenceTests(unittest.TestCase):
 def test_ogg_frames_are_preserved_for_worker_and_pcm_preview_is_available(self):
  import json,tempfile
  import numpy as np
  import soundfile as sf
  from pathlib import Path
  from unittest.mock import patch
  from studio.tts.stream_reference import resolve_stream_reference
  from studio.tts.omnivoice import sha256
  from studio.audio.wav import wav_info
  with tempfile.TemporaryDirectory() as temporary:
   root=Path(temporary);source=root/'original.ogg';sf.write(source,np.sin(np.arange(32000)/20)*.2,16000,format='OGG',subtype='VORBIS')
   alignment=root/'alignment.json';alignment.write_text('{}')
   row={'source_path':str(source),'source_sha256':sha256(source),'input_path':str(alignment),'input_sha256':sha256(alignment),'start':.1,'end':.8,'canonical_text':'Original line','reference_text':'Original line','container':'CUTSCENE','track':'Track_001','gid':'MAIN:12345678','status':'ALIGNMENT_CANDIDATE','timing_authority':'TEST_CANDIDATE'}
   with patch('studio.audio.stream_catalog.lookup_window',return_value=row):
    ref=resolve_stream_reference({'stable_audio_id':'CUTSCENE:test'},root,'Original line')
    self.assertEqual(wav_info(ref['audition_path'])['fmt'],1)
    with sf.SoundFile(source) as original:
     original.seek(1600);expected=original.read(11200,dtype='float32',always_2d=True)
    actual,_=sf.read(ref['path'],dtype='float32',always_2d=True)
    self.assertTrue(np.array_equal(expected,actual))
    Path(ref['path']).write_bytes(b'changed')
    with self.assertRaises((ValueError,RuntimeError)):resolve_stream_reference({'stable_audio_id':'CUTSCENE:test'},root,'Original line')
if __name__=='__main__':unittest.main()
