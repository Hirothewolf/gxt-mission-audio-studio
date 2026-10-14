import configparser
import contextlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf
from studio.audio.timeline_project import TimelineProject
from studio.audio import scene_publish as publish
from studio.audio.stream_install import _proof_path
from studio.core.utils import sha256_arquivo
from studio.saat.audio import GameAudio


class ScenePublishTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.mod = self.root/'mod'; self.archive = self.mod/'audio/streams/BEATS'
        self.lookup = self.mod/'audio/CONFIG/TrakLkup.dat'
        self.archive.parent.mkdir(parents=True); self.lookup.parent.mkdir(parents=True)
        self.lookup.write_bytes(bytes([4])+bytes(11)+bytes([8])+bytes(11))
        self.original = self.root/'original.wav'; self.take = self.root/'take.wav'
        sf.write(self.original, np.full((100,1), .1), 8000, subtype='PCM_16')
        sf.write(self.take, np.full((50,1), .2), 8000, subtype='PCM_16')
        contents = {'Track_%03d'%i:self.original.read_bytes().hex() for i in range(1,11)}
        self.archive.write_text(json.dumps(contents))
        self.original_archive = self.root/'original_bank'; self.original_archive.write_bytes(b'original')
        self.project = TimelineProject(8000,1)
        self.project.add_wav(self.take)
        self.project.data['scene'] = dict(container='BEATS',track='Track_010',
            original_path=str(self.original), original_sha256=sha256_arquivo(self.original),
            original_archive_path=str(self.original_archive),original_archive_sha256=sha256_arquivo(self.original_archive),
            installed_archive_sha256=sha256_arquivo(self.archive))
        self.ga = SimpleNamespace(mod_ativo=lambda:'mod',_safe_mod_dir=lambda mod:self.mod,
            _dentro=lambda root,path:path.is_relative_to(root),saat=lambda:{'stream':'saat'},
            _native_install_lock=contextlib.nullcontext,_game_is_running=lambda:False,
            _publish_native_pair=GameAudio._publish_native_pair)
        self.context = SimpleNamespace(project_root=self.root,game_audio=self.ga)
        self.cancel = threading.Event(); self.corrupt = False; self.bad_lookup = False

    def runner(self, executable, args, cwd, log):
        if str(executable) == 'ffmpeg':
            shutil.copy2(args[args.index('-i')+1],args[-1]);return
        if args[0] == '-e':
            bank, export = Path(args[1]),Path(args[2]); folder=export/bank.name;folder.mkdir()
            data=json.loads(bank.read_text()); ini=configparser.ConfigParser()
            for name, raw in data.items():
                path=folder/(name+'.ogg');path.write_bytes(bytes.fromhex(raw))
                if self.corrupt and export.name=='roundtrip' and name=='Track_001':path.write_bytes(b'broken')
                ini[name]={'filename':path.name}
            with (folder/'stream_import.ini').open('w') as out:ini.write(out)
        else:
            bank,recipe,lookup=map(Path,args[1:]);ini=configparser.ConfigParser();ini.read(recipe)
            bank.write_text(json.dumps({name:Path(ini.get(name,'filename')).read_bytes().hex() for name in ini.sections()}))
            if self.bad_lookup:
                raw=bytearray(lookup.read_bytes());raw[15]=1;lookup.write_bytes(raw)

    def call(self, progress=None):
        with patch.object(publish,'_run',side_effect=self.runner),patch.object(publish.shutil,'which',return_value='ffmpeg'):
            return publish.publish_scene(self.context,self.project,progress=progress,cancel=self.cancel)

    def test_full_publish_pads_preserves_other_tracks_and_invalidates_only_changed_scene(self):
        proof=_proof_path(self.root,'mod');proof.parent.mkdir(parents=True)
        old=sha256_arquivo(self.archive)
        proof.write_text(json.dumps({'lines':{'changed':dict(container='BEATS',track='Track_010',archive_sha256=old),
            'other':dict(container='BEATS',track='Track_001',archive_sha256=old)},
            'layers':{'old':dict(container='BEATS',track='Track_010',archive_sha256=old)}}))
        before=json.loads(self.archive.read_text());response=self.call();after=json.loads(self.archive.read_text())
        self.assertTrue(response['ok'])
        for name in before:
            if name!='Track_010':self.assertEqual(before[name],after[name])
        path=self.root/'changed.wav';path.write_bytes(bytes.fromhex(after['Track_010']))
        pcm,rate=sf.read(path,always_2d=True)
        self.assertEqual((100,1),pcm.shape);self.assertEqual(8000,rate)
        np.testing.assert_array_equal(pcm[50:],0)
        data=json.loads(proof.read_text());self.assertNotIn('changed',data['lines']);self.assertFalse(data['layers'])
        self.assertEqual(response['archive_sha256'],data['lines']['other']['archive_sha256'])
        self.assertEqual(before,json.loads((Path(response['backup'])/'BEATS').read_text()))

    def test_cutscene_141_tracks_preserved(self):
        old_archive=self.archive
        self.archive=self.archive.with_name('CUTSCENE')
        self.archive.write_text(json.dumps({'Track_%03d'%i:self.original.read_bytes().hex() for i in range(1,142)}))
        old_archive.unlink()
        self.project.data['scene'].update(container='CUTSCENE',track='Track_141',
            installed_archive_sha256=sha256_arquivo(self.archive))
        result=self.call()
        self.assertTrue(result['ok'])
        proof=json.loads(Path(result['proof_path']).read_text())
        self.assertEqual(140,proof['unchanged_tracks_byte_exact'])

    def test_muted_voices_rejected(self):
        self.project.update_track(self.project.data['tracks'][0]['id'],mute=True)
        with self.assertRaisesRegex(ValueError,'audíveis'):self.call()

    def test_background_only_edit_publishes_without_pt_generation(self):
        clip=self.project.data['clips'][0]
        self.project.update_track(clip['track_id'],role='ambience')
        self.project.update_clip(clip['id'],position=10,metadata={'scene_background':True})
        result=self.call()
        self.assertTrue(result['ok'])
        data=json.loads(self.archive.read_text())
        path=self.root/'background_result.wav';path.write_bytes(bytes.fromhex(data['Track_010']))
        pcm,_=sf.read(path)
        np.testing.assert_array_equal(pcm[:10],0)
        self.assertTrue(np.any(pcm[10:60]))
        np.testing.assert_array_equal(pcm[60:],0)

    def test_silent_pt_not_hidden_by_audible_background(self):
        sf.write(self.take,np.zeros(50),8000,subtype='PCM_16')
        scene=self.project.data['scene']
        self.project=TimelineProject(8000,1)
        self.project.add_wav(self.take)
        self.project.add_wav(self.original,role='ambience',metadata={'scene_background':True})
        self.project.data['scene']=scene
        with self.assertRaisesRegex(ValueError,'vozes estão silenciosas'):self.call()

    def test_stale_archive_pin_rejected(self):
        self.archive.write_text(self.archive.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'mudou desde'):self.call()

    def test_silent_voice_rejected(self):
        sf.write(self.take,np.zeros(50),8000,subtype='PCM_16')
        self.project=TimelineProject(8000,1);self.project.add_wav(self.take)
        self.project.data['scene']=dict(container='BEATS',track='Track_010',original_path=str(self.original),
            original_sha256=sha256_arquivo(self.original),original_archive_path=str(self.original_archive),
            original_archive_sha256=sha256_arquivo(self.original_archive),installed_archive_sha256=sha256_arquivo(self.archive))
        before=self.archive.read_bytes()
        with self.assertRaisesRegex(ValueError,'silencios'):self.call()
        self.assertEqual(before,self.archive.read_bytes())

    def test_overflow_rejected(self):
        self.project.update_clip(self.project.data['clips'][0]['id'],position=60)
        with self.assertRaisesRegex(ValueError,'ultrapassa'):self.call()

    def test_untouched_track_corruption_rejected(self):
        self.corrupt=True;before=self.archive.read_bytes()
        with self.assertRaisesRegex(ValueError,'alterou bytes'):self.call()
        self.assertEqual(before,self.archive.read_bytes())

    def test_changed_track_container_metadata_can_change_with_identical_pcm(self):
        runner = self.runner
        def repack(executable, args, cwd, log):
            runner(executable, args, cwd, log)
            if args[0] == '-e' and Path(args[2]).name == 'roundtrip':
                path = Path(args[2]) / 'BEATS/Track_010.ogg'
                path.write_bytes(path.read_bytes() + b'JUNK\x04\x00\x00\x00test')
        with patch.object(publish, '_run', side_effect=repack), patch.object(publish.shutil, 'which', return_value='ffmpeg'):
            result = publish.publish_scene(self.context, self.project, cancel=self.cancel)
        self.assertTrue(result['ok'])

    def test_published_scene_has_fresh_auditions_separate_from_voice_layers(self):
        clip = self.project.data['clips'][0]
        clip['metadata'].update(table='MAIN', gxt_hash=42, window_role='take')
        result = self.call()
        from studio.audio.stream_install import installed_stream_path
        self.ga._stream_proof_cache = {}
        info = dict(pak='BEATS', stream={'track':'Track_010'}, gxt_stable_id='MAIN:0000002A')
        path = installed_stream_path(info, self.root, self.ga)
        self.assertTrue(path.is_file())
        proof = json.loads(_proof_path(self.root, 'mod').read_text())
        self.assertNotIn('MAIN:0000002A', proof['lines'])
        self.assertEqual(proof['scenes']['BEATS/Track_010']['archive_sha256'], result['archive_sha256'])

    def test_scene_auditions_invalidated_only_for_changed_tracks(self):
        from studio.audio.stream_install import refresh_scene_evidence
        proof = {'scenes': {'BEATS/Track_001': {'archive_sha256':'old','auditions':{'key':{'archive_sha256':'old'}}},
                            'BEATS/Track_010': {'archive_sha256':'old','auditions':{}},
                            'CUTSCENE/Track_001': {'archive_sha256':'different'}}}
        refresh_scene_evidence(proof, 'BEATS', 'old', 'new', {'Track_010'})
        self.assertNotIn('BEATS/Track_010', proof['scenes'])
        self.assertEqual(proof['scenes']['BEATS/Track_001']['auditions']['key']['archive_sha256'],'new')
        self.assertEqual(proof['scenes']['CUTSCENE/Track_001']['archive_sha256'],'different')

    def test_current_mod_duration_can_be_longer_than_original(self):
        extra = self.root / 'longer.wav'
        sf.write(extra, np.full((150, 1), .1), 8000, subtype='PCM_16')
        contents = json.loads(self.archive.read_text())
        contents['Track_010'] = extra.read_bytes().hex()
        self.archive.write_text(json.dumps(contents))
        self.project.data['scene']['installed_archive_sha256'] = sha256_arquivo(self.archive)
        background = self.project.add_wav(extra, role='ambience', metadata={'scene_background': True})
        self.project.update_track(self.project.clip(background)['track_id'], mute=False)
        result = self.call()
        proof = json.loads(Path(result['proof_path']).read_text())
        self.assertEqual(proof['native_frames'], 150)

    def test_muted_original_past_current_destination_does_not_force_cut(self):
        longer = self.root / 'comparison.wav'
        sf.write(longer, np.full((200, 1), .1), 8000, subtype='PCM_16')
        self.project.add_wav(longer, role='original')
        result = self.call()
        self.assertTrue(result['ok'])

    def test_other_lookup_bank_change_rejected(self):
        self.bad_lookup=True;before=self.lookup.read_bytes()
        with self.assertRaisesRegex(ValueError,'outro banco'):self.call()
        self.assertEqual(before,self.lookup.read_bytes())

    def test_cancel_before_publication_preserves_pair(self):
        before=self.archive.read_bytes()
        def progress(n,total,text):
            if n==4:self.cancel.set()
        with self.assertRaises(InterruptedError):self.call(progress)
        self.assertEqual(before,self.archive.read_bytes())

    def test_concurrent_archive_change_is_preserved(self):
        def progress(n,total,text):
            if n==4:self.archive.write_bytes(b'external edit')
        with self.assertRaisesRegex(ValueError,'fonte mudou'):self.call(progress)
        self.assertEqual(b'external edit',self.archive.read_bytes())

    def test_proof_write_failure_rolls_back_native_pair(self):
        before=self.archive.read_bytes();lookup=self.lookup.read_bytes();real=publish.json_atomico
        def writer(path,data):
            if Path(path)==_proof_path(self.root,'mod'):raise OSError('disk failed')
            real(path,data)
        with patch.object(publish,'json_atomico',side_effect=writer):
            with self.assertRaisesRegex(OSError,'disk failed'):self.call()
        self.assertEqual(before,self.archive.read_bytes());self.assertEqual(lookup,self.lookup.read_bytes())

if __name__=='__main__':unittest.main()
