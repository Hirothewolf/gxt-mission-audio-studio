import math
import shutil
import threading
import tempfile
import unittest
import wave
from pathlib import Path

from studio.audio import dialogue
from studio.audio.cutscene_studio import import_replacement, render_track


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class DialogueServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'incoming.wav'
        with wave.open(str(self.source), 'wb') as f:
            f.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            f.writeframes(b'\x10\x00' * 16000)
        self.track = dialogue.open_source(self.root, self.source)

    def tearDown(self):
        self.tmp.cleanup()

    def segment(self, **kwargs):
        return dict(id='a'*32, start=.2, end=.7, speaker='CJ', speaker_confirmed=True,
                    text='Texto hipertraduzido', reference_transcript='Original', **kwargs)

    def test_owned_snapshot_survives_external_edit_and_roundtrip(self):
        segment = self.segment()
        saved = dialogue.save_draft(self.root, self.track, [segment])
        self.source.write_bytes(b'changed external source')
        opened = dialogue.load_draft(self.root, saved)
        self.assertEqual(opened['segments'][0], segment)
        self.assertEqual(opened['track_data']['pcm'], self.track['pcm'])
        self.assertEqual(opened['track_data']['source_kind'], 'imported')
        self.assertEqual(opened['speaker_layout'], 'unreviewed')

    def test_confirmed_multiple_annotations_separate_from_container(self):
        second = dict(id='b'*32, start=1, end=1.5, speaker='Ryder',
                      speaker_confirmed=True, text='Outro texto')
        saved = dialogue.save_draft(self.root, self.track, [self.segment(), second])
        opened = dialogue.load_draft(self.root, saved)
        self.assertEqual(opened['speaker_layout'], 'multiple_annotated')
        self.assertEqual(opened['track_data']['source_kind'], 'imported')

    def test_duplicate_ids_overlap_nan_rejected(self):
        for second in (dict(self.segment()), dict(self.segment(), id='b'*32),
                       dict(self.segment(), id='b'*32, start=math.nan)):
            with self.subTest(second=second), self.assertRaises(ValueError):
                dialogue.save_draft(self.root, self.track, [self.segment(), second])

    def test_changed_owned_source_and_traversal_rejected(self):
        saved = dialogue.save_draft(self.root, self.track, [self.segment()])
        with self.assertRaises(ValueError):
            dialogue.save_draft(self.root, self.track, [self.segment()], draft_id='../outside')
        copied = self.root / 'elsewhere.json'
        copied.write_bytes(saved.read_bytes())
        with self.assertRaises(ValueError):
            dialogue.load_draft(self.root, copied)
        Path(self.track['source_path']).write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            dialogue.load_draft(self.root, saved)

    def test_replacement_copied_to_owned_storage_and_render_outside_untouched(self):
        take = self.root / 'take.wav'
        with wave.open(str(take), 'wb') as f:
            f.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            f.writeframes(b'\x20\x00' * 4000)
        saved = dialogue.save_draft(self.root, self.track,
                                   [self.segment(replacement=import_replacement(take))])
        take.write_bytes(b'external file changed')
        opened = dialogue.load_draft(self.root, saved)
        row = opened['segments'][0]
        output = self.root / 'full.wav'
        render_track(opened['track_data'], [row['replacement']],
                     [{'start': row['start'], 'end': row['end']}], output, background_mode='preserve_original')
        with wave.open(str(output), 'rb') as f:
            pcm = f.readframes(f.getnframes())
        self.assertEqual(len(pcm), len(self.track['pcm']))
        self.assertEqual(pcm[:3200], self.track['pcm'][:3200])
        self.assertEqual(pcm[11200:], self.track['pcm'][11200:])
        self.assertNotEqual(pcm[3200:11200], self.track['pcm'][3200:11200])

    def test_unconfirmed_labels_not_multispeaker_proof(self):
        second = dict(id='b'*32, start=1, end=1.5, speaker='Ryder',
                      speaker_confirmed=False, text='Texto')
        saved = dialogue.save_draft(self.root, self.track, [self.segment(), second])
        self.assertEqual(dialogue.load_draft(self.root, saved)['speaker_layout'], 'unreviewed')

    def test_changed_gxt_association_rejected_on_reopen(self):
        gxt = self.root / 'mod.gxt'
        gxt.write_bytes(b'mod text snapshot')
        saved = dialogue.save_draft(self.root, self.track,
                    [self.segment(gxt={'gxt_path': str(gxt), 'text': 'Texto'})])
        gxt.write_bytes(b'another revision')
        with self.assertRaisesRegex(ValueError, 'GXT'):
            dialogue.load_draft(self.root, saved)

    def test_cancelled_save_does_not_publish(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(RuntimeError, 'cancelada'):
            dialogue.save_draft(self.root, self.track, [self.segment()], cancel_event=cancel)
        self.assertFalse(list((self.root / 'workspace/dialogue_studio/drafts').glob('*.json')))


if __name__ == '__main__':
    unittest.main()
