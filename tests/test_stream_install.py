import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

from studio.audio.stream_install import replace_regions, validate_lookup, destination_window, install_stream_rows, _prior_layers, installed_stream_path, _proof_path


class StreamInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wav = self.root/'take.wav'
        sf.write(self.wav, np.full(100, .2, dtype=np.float32), 1000, subtype='FLOAT')

    def entry(self, line, start, end):
        return dict(line_id=line, start=start, end=end, path=str(self.wav))

    def test_only_selected_region_replaced_and_short_take_keeps_silence(self):
        source = np.full((1000, 2), .1, dtype=np.float32)
        out, report = replace_regions(source, 1000, [self.entry('a', .2, .4)])
        np.testing.assert_array_equal(out[:200], source[:200])
        np.testing.assert_array_equal(out[400:], source[400:])
        np.testing.assert_allclose(out[200:300], .2)
        np.testing.assert_array_equal(out[300:400], 0)
        self.assertEqual(report[0]['speech_frames_cut'], 0)

    def test_overlapping_takes_mixed_without_last_take_erasing_first(self):
        source = np.zeros((1000, 1), dtype=np.float32)
        out, _ = replace_regions(source, 1000, [self.entry('a', .2, .3), self.entry('b', .25, .4)])
        np.testing.assert_allclose(out[250:300], .4)
        np.testing.assert_allclose(out[300:350], .2)

    def test_full_long_take_is_not_cut_or_accelerated(self):
        out, report = replace_regions(np.zeros((1000,1),np.float32), 1000, [self.entry('a', .2, .25)])
        self.assertEqual(report[0]['placed_frames'], 100)
        self.assertEqual(np.count_nonzero(out), 100)
        self.assertFalse(report[0]['post_time_stretch'])

    def test_replacing_previous_long_take_removes_its_tail(self):
        e = self.entry('a', .2, .25); e['previous_regions'] = [[.2,.4]]
        out, _ = replace_regions(np.full((1000,1),.1,np.float32),1000,[e])
        np.testing.assert_array_equal(out[300:400], 0)
        np.testing.assert_allclose(out[400:],.1)

    def test_end_overflow_rejected_instead_of_cutting(self):
        with self.assertRaises(ValueError):
            replace_regions(np.zeros((1000,1),np.float32),1000,[self.entry('a',.95,1)])

    def test_overlapping_installed_neighbour_restored_only_inside_cleared_region(self):
        source=np.zeros((1000,1),np.float32);source[250:350]=.2
        out,_=replace_regions(source,1000,[self.entry('a',.2,.3)],
                              [{'path':str(self.wav),'start':.25}])
        np.testing.assert_allclose(out[250:300],.4)
        np.testing.assert_allclose(out[300:350],.2)

    def test_unrelated_lookup_record_changes_are_rejected(self):
        before = bytes([4])+bytes(11)+bytes([8])+bytes(11)
        good = bytearray(before);good[3]=1
        validate_lookup(before,good,4)
        bad = bytearray(good);bad[15]=1
        with self.assertRaises(ValueError):validate_lookup(before,bad,4)

    def test_destination_uses_caption_clock_not_voice_reference_crop(self):
        x=destination_window({'stable_audio_id':'CUTSCENE:X:TEST','pak':'CUTSCENE',
            'stream':{'track':'Track_001'},'caption_start_ms':5300,'caption_duration_ms':1450},self.root)
        self.assertEqual((x['start'],x['end']),(5.3,6.75))

    def test_changed_or_unapproved_take_cannot_reach_bank_writer(self):
        store=Mock(root=self.root);store.approved_path.return_value=None
        with patch('studio.audio.stream_install._install_bank') as writer:
            r=install_stream_rows(store,[{'line_id':'INTRO1|00000001','text':'Texto','path':'x'}],Mock(),'mod')
        self.assertEqual(len(r['skipped']),1);writer.assert_not_called()

    def test_old_production_positions_require_exact_current_bank_pin(self):
        import json
        report=self.root/'deliverables/cutscenes_all_recovered_safe_20261011_v1/staging_report.json'
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({'staged_assets':{'audio/streams/CUTSCENE':'current'},
            'inserted':[{'gid':'MAIN:00000001','track':'Track_001','start':5.41,'duration':.88,
                         'path':str(self.wav),'sha256':'wav-proof'}]}))
        self.assertEqual(_prior_layers(self.root,'CUTSCENE','other',{'lines':{}}),{})
        layers=_prior_layers(self.root,'CUTSCENE','current',{'lines':{}})
        row=layers['CUTSCENE/Track_001/MAIN:00000001']
        self.assertAlmostEqual(row['end'],6.29)

    def test_installed_label_requires_live_bank_and_audition_hashes(self):
        import json
        from studio.core.utils import sha256_arquivo
        mod=self.root/'mod';bank=mod/'audio/streams/BEATS';bank.parent.mkdir(parents=True)
        bank.write_bytes(b'installed bank')
        proof=_proof_path(self.root,'mod');proof.parent.mkdir(parents=True)
        proof.write_text(json.dumps({'lines':{'INTRO1:00000001':{
            'container':'BEATS','track':'Track_010','archive_sha256':sha256_arquivo(bank),
            'audition_path':str(self.wav),'audition_sha256':sha256_arquivo(self.wav)}}}))
        info={'stable_audio_id':'POLICE:PRO@24','gxt_stable_id':'INTRO1:00000001'}
        game=Mock();game._safe_mod_dir.return_value=mod;game._stream_proof_cache={}
        self.assertEqual(installed_stream_path(info,self.root,game,'mod'),self.wav)
        bank.write_bytes(b'changed bank')
        self.assertIsNone(installed_stream_path(info,self.root,game,'mod'))


if __name__=='__main__':unittest.main()
