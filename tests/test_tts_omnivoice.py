import hashlib
import importlib.util
import json
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from studio.tts.omnivoice import (
    build_command,
    coordinates_from_record,
    is_nonverbal_marker,
    load_accepted_cj_reference,
    resolve_original_reference,
)


class OmniVoiceReferenceTests(unittest.TestCase):
    def test_nonverbal_markers_are_not_treated_as_dialogue(self):
        self.assertTrue(is_nonverbal_marker('~z~(SCREAM)'))
        self.assertTrue(is_nonverbal_marker('[unintelligible]'))
        self.assertFalse(is_nonverbal_marker('I scream when I see it.'))

    def record(self):
        return {
            'pak': 'SCRIPT',
            'stable_audio_id': 'SCRIPT:B090:S024',
            'bank_index': 90,
            'sound_index': 24,
            'bank_num': 91,
            'sound_num': 25,
            'source_wav_path': r'D:\project\extracted\SCRIPT\Bank_091\sound_025.wav',
            'source_text_en': 'Original English line.',
        }

    def test_coordinate_representations_agree_and_convert_to_saat_indices(self):
        self.assertEqual(coordinates_from_record(self.record()), ('SCRIPT', 91, 25))

    def test_conflicting_bank_slot_is_rejected(self):
        record = self.record()
        record['sound_num'] = 24
        with self.assertRaisesRegex(ValueError, 'conflit'):
            coordinates_from_record(record)

    def test_modloader_or_non_native_path_is_never_a_reference(self):
        record = self.record()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / 'cache'
            modloader = root / 'game' / 'modloader' / 'voice' / 'audio' / 'SFX' / 'SCRIPT' / 'Bank_091'
            modloader.mkdir(parents=True)
            wav = modloader / 'sound_025.wav'
            wav.write_bytes(b'mod voice')
            game_audio = SimpleNamespace(
                indice={('SCRIPT', 91, 25): str(wav)},
                cache_dir=lambda: cache,
                _cache_original_atual=lambda pak: True,
            )
            with self.assertRaisesRegex(ValueError, 'cache original'):
                resolve_original_reference(record, game_audio)

    def test_exact_original_cache_path_and_current_archive_are_required(self):
        record = self.record()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / 'cache'
            original = cache / 'export' / 'SCRIPT' / 'Bank_091' / 'sound_025.wav'
            original.parent.mkdir(parents=True)
            original.write_bytes(b'base game voice')
            game_audio = SimpleNamespace(
                indice={('SCRIPT', 91, 25): str(original)},
                cache_dir=lambda: cache,
                _cache_original_atual=lambda pak: pak == 'SCRIPT',
                _assinatura_fonte=lambda pak: {'SCRIPT': ['archive-sha']},
            )
            result = resolve_original_reference(record, game_audio, project_root=root)
            self.assertEqual(result['path'], str(original.resolve()))
            self.assertEqual(result['sha256'], hashlib.sha256(original.read_bytes()).hexdigest())
            self.assertEqual(result['coordinates'], ['SCRIPT', 91, 25])

    def test_ambiguous_or_missing_mapping_fails_closed(self):
        record = self.record()
        with self.assertRaisesRegex(ValueError, 'ausente'):
            coordinates_from_record({})
        record['pak'] = 'SPC_PA'
        record['stable_audio_id'] = 'SPC_PA:B090:S024'
        record['source_wav_path'] = r'D:\project\extracted\SPC_PA\Bank_091\sound_025.wav'
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / 'cache'
            game_audio = SimpleNamespace(indice={}, cache_dir=lambda: cache,
                                         _cache_original_atual=lambda _pak: False)
            with self.assertRaisesRegex(ValueError, 'indexado'):
                resolve_original_reference(record, game_audio)


class OmniVoiceAdapterTests(unittest.TestCase):
    def test_command_preserves_reference_hash_provenance_and_native_parameters(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reference = root / 'original.wav'
            reference.write_bytes(b'original sample')
            python = root / 'python.exe'
            script = root / 'tools' / 'omnivoice_tts.py'
            python.write_bytes(b'cached runtime')
            script.parent.mkdir()
            script.write_text('# cached adapter')
            output = root / 'workspace' / 'take.wav'
            base_gxt = root / 'american.gxt'
            base_gxt.write_bytes(b'original GXT')
            provenance = {
                'source_kind': 'original_game_saat_cache',
                'source_path': str(reference),
                'source_sha256': hashlib.sha256(reference.read_bytes()).hexdigest(),
                'gxt_id': 'INTRO1:DEFE0050', 'stable_audio_id': 'SCRIPT:B090:S024',
                'base_english_gxt_path': str(base_gxt),
                'base_english_gxt_sha256': hashlib.sha256(base_gxt.read_bytes()).hexdigest(),
                'base_english_text': 'Original English.', 'base_english_gxt_id': 'INTRO1:DEFE0050',
            }
            command = build_command(
                root, reference, 'Original English.', '  Texto PT.  ', output,
                provenance, speed=1.2, duration=3.4, python_path=python,
                script_path=script,
            )
            request_path = Path(command[command.index('--request') + 1])
            request = json.loads(request_path.read_text(encoding='utf-8'))
            self.assertEqual(request['reference_sha256'], hashlib.sha256(reference.read_bytes()).hexdigest())
            self.assertEqual(request['reference_text'], 'Original English.')
            self.assertEqual(request['target_text'], '  Texto PT.  ')
            self.assertEqual(request['spoken_target_text'], 'Texto PT.')
            self.assertEqual(request['language'], 'pt')
            self.assertEqual((request['speed'], request['duration']), (1.2, 3.4))
            self.assertFalse(request['postprocess_output'])
            self.assertEqual(request['provenance'], provenance)
            self.assertIsInstance(request['seed'], int)
            self.assertTrue(0 <= request['seed'] <= 2147483647)
            seeds = []
            for number, seed in enumerate((None, None, 17, 17)):
                cmd = build_command(root, reference, 'Original English.', 'Texto PT.', root / f'random{number}.wav',
                    provenance, python_path=python, script_path=script, seed=seed)
                seeds.append(json.loads(Path(cmd[-1]).read_text())['seed'])
            self.assertNotEqual(seeds[0], seeds[1])
            self.assertEqual(seeds[2:], [17, 17])
            stale = dict(provenance)
            for key in ('base_english_gxt_path', 'base_english_gxt_sha256',
                        'base_english_text', 'base_english_gxt_id'):
                stale.pop(key)
            with self.assertRaisesRegex(ValueError, 'editor aberto está desatualizado'):
                build_command(root, reference, 'Original English.', 'Texto PT.', root / 'stale.wav',
                              stale, python_path=python, script_path=script)
            with self.assertRaisesRegex(ValueError, 'marcador não verbal'):
                build_command(root, reference, '(SCREAM)', 'Texto PT.', root / 'take2.wav',
                              provenance, python_path=python, script_path=script)

    def test_registry_exposes_cached_omnivoice_as_single_utterance_engine(self):
        from studio import config
        from studio.tts.registry import OmniVoiceEngine, select_engine
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            python = root / 'deliverables' / 'omnivoice_gta_four_actor_preview_v1' / 'env' / 'Scripts' / 'python.exe'
            python.parent.mkdir(parents=True)
            python.write_bytes(b'cached runtime')
            with unittest.mock.patch.dict(config.CFG, {'engine_padrao': 'omnivoice', 'tts_engines': {}}, clear=False):
                engine = select_engine(root=root)
            self.assertIsInstance(engine, OmniVoiceEngine)
            self.assertEqual(engine.python, python.resolve())
            self.assertFalse(engine.capabilities.batch_generation)
            with self.assertRaisesRegex(RuntimeError, 'individual'):
                engine.build_command('INTRO1', 'snapshot.gxt', root / 'output')

    def test_accepted_cj_reference_is_original_same_utterance_and_hash_pinned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = root / 'reports' / 'studio_rework_20261008' / 'omnivoice_cj_utterance_reference_test_v1'
            report.mkdir(parents=True)
            refs = root / 'deliverables' / 'omnivoice_cj_utterance_reference_test_v1' / 'references'
            refs.mkdir(parents=True)
            source = root / 'native' / 'Track_078.wav'
            source.parent.mkdir()
            source.write_bytes(b'original cutscene source')
            ref = refs / 'Track_078_04_aligned_original.wav'
            ref.write_bytes(b'original CJ utterance crop')
            record = {
                'id': 'Track_078:04', 'reference_path': str(ref),
                'reference_sha256': hashlib.sha256(ref.read_bytes()).hexdigest(),
                'reference_source': str(source),
                'reference_source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'reference_text': "Hey, hey! Big Smoke, it's me, Carl! Chill, chill!",
                'alignment_authority': 'NATIVE_CUE_AND_BLIND_ASR_WORD_BOUNDARIES_WITH_MARGINS',
                'reference_start_seconds': 48.35, 'reference_end_seconds': 51.95,
                'reference_relation': 'SAME_ORIGINAL_UTTERANCE',
            }
            (report / 'Track_078_04_aligned_receipt.json').write_text(json.dumps({
                'id': 'Track_078:04', 'reference_sha256': record['reference_sha256'],
                'reference_text': record['reference_text'], 'seed': 2026100871,
            }), encoding='utf-8')
            record['source_kind'] = 'accepted_original_cutscene_utterance'
            payload = {'records': [record]}
            (report / 'aligned_comparison_input.json').write_text(json.dumps(payload), encoding='utf-8')
            selected = load_accepted_cj_reference(root, target_identity='CJ', identity_confirmed=True)
            self.assertEqual(selected['path'], str(ref.resolve()))
            self.assertEqual(selected['reference_text'], record['reference_text'])
            self.assertEqual(selected['provenance']['reference_source_sha256'], record['reference_source_sha256'])
            # The accepted voice's old generation seed does not bind a new take.
            worker_path = Path(__file__).resolve().parents[1] / 'tools/omnivoice_tts.py'
            spec = importlib.util.spec_from_file_location('cj_seed_worker', worker_path)
            worker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(worker)
            base_gxt = root / 'american.gxt'
            base_gxt.write_bytes(b'original GXT')
            provenance = dict(selected['provenance'], source_kind=selected['reference_kind'],
                source_path=str(source), source_sha256=record['reference_source_sha256'],
                base_english_gxt_path=str(base_gxt), base_english_gxt_sha256=hashlib.sha256(base_gxt.read_bytes()).hexdigest(),
                base_english_text='Original target.', base_english_gxt_id='INTRO1:DEFE0050')
            request = dict(project_root=str(root), reference_path=str(ref), reference_sha256=selected['sha256'],
                reference_text=selected['reference_text'], target_text='Texto PT.', spoken_target_text='Texto PT.',
                output_path=str(root / 'new-seed.wav'), seed=7, duration=None, speed=1., language='pt',
                postprocess_output=False, window_seconds=2., provenance=provenance)
            model = SimpleNamespace(create_voice_clone_prompt=Mock(return_value='prompt'),
                generate=Mock(return_value=[worker.np.full(2400, .1, dtype=worker.np.float32)]))
            result = worker.generate(request, model=model, model_revision='fixture')
            self.assertEqual(result['seed'], 7)
            self.assertEqual(result['status'], 'UNREVIEWED_WITHIN_WINDOW')
            request['output_path'] = str(root / 'tampered.wav')
            request['reference_text'] = 'Different original words.'
            with self.assertRaisesRegex(ValueError, 'transcrição'):
                worker.generate(request, model=model, model_revision='fixture')
            with self.assertRaisesRegex(ValueError, 'CJ aceita'):
                load_accepted_cj_reference(root, target_identity='Sweet', identity_confirmed=True)


class OmniVoiceWorkerTests(unittest.TestCase):
    def test_fake_runtime_receives_portuguese_and_native_controls_without_postprocessing(self):
        path = Path(__file__).resolve().parents[1] / 'tools' / 'omnivoice_tts.py'
        spec = importlib.util.spec_from_file_location('omnivoice_worker_fixture', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reference = root / 'cache' / 'export' / 'SCRIPT' / 'Bank_001' / 'sound_001.wav'
            reference.parent.mkdir(parents=True)
            reference.write_bytes(b'reference')
            archive = root / 'original.pak'
            archive.write_bytes(b'archive')
            base_gxt = root / 'american.gxt'
            base_gxt.write_bytes(b'original GXT')
            base_gxt = root / 'american.gxt'
            base_gxt.write_bytes(b'original GXT')
            output = root / 'take.wav'
            request = {
                'reference_path': str(reference),
                'reference_sha256': hashlib.sha256(reference.read_bytes()).hexdigest(),
                'reference_text': 'Original English.', 'target_text': 'Texto PT.', 'spoken_target_text': 'Texto PT.',
                'output_path': str(output), 'language': 'pt', 'speed': 1.15,
                'duration': 2.75, 'seed': 2026100871, 'num_step': 32, 'postprocess_output': False,
                'provenance': {
                    'source_kind': 'original_game_saat_cache',
                    'source_path': str(reference),
                    'source_sha256': hashlib.sha256(reference.read_bytes()).hexdigest(),
                    'gxt_id': 'INTRO1:DEFE0050',
                    'coordinates': ['SCRIPT', 1, 1], 'cache_root': str(root / 'cache'),
                    'archive_path': str(archive), 'archive_stat': [archive.stat().st_size, archive.stat().st_mtime_ns],
                    'base_english_gxt_path': str(base_gxt),
                    'base_english_gxt_sha256': hashlib.sha256(base_gxt.read_bytes()).hexdigest(),
                    'base_english_gxt_id': 'INTRO1:DEFE0050', 'base_english_text': 'Original English.',
                    'base_english_gxt_path': str(base_gxt),
                    'base_english_gxt_sha256': hashlib.sha256(base_gxt.read_bytes()).hexdigest(),
                    'base_english_gxt_id': 'INTRO1:DEFE0050', 'base_english_text': 'Original English.',
                },
            }
            model = SimpleNamespace(
                create_voice_clone_prompt=Mock(return_value='prompt'),
                generate=Mock(return_value=[module.np.full(24000, 0.1, dtype=module.np.float32)]),
            )
            progress = io.StringIO()
            with redirect_stdout(progress):
                result = module.generate(request, model=model, model_revision='fixture')
            stages = [json.loads(line)['stage'] for line in progress.getvalue().splitlines()]
            self.assertEqual(stages, ['generating', 'saving'])
            kwargs = model.generate.call_args.kwargs
            self.assertEqual(kwargs['language'], 'pt')
            self.assertEqual(kwargs['speed'], 1.15)
            self.assertEqual(kwargs['duration'], 2.75)
            self.assertEqual(kwargs['text'], 'Texto PT.')
            self.assertIs(kwargs['postprocess_output'], False)
            self.assertEqual(model.create_voice_clone_prompt.call_args.kwargs['ref_text'], 'Original English.')
            self.assertEqual(result['reference_sha256'], request['reference_sha256'])
            self.assertFalse(result['post_synthesis_time_stretch'])
            self.assertFalse(result['speech_frames_cut'])
            self.assertEqual(result['seed'], 2026100871)
            self.assertTrue(output.is_file())
            self.assertGreater(result['rms'], 0)
            self.assertEqual(result['clipping_ratio'], 0)
            from unittest.mock import patch
            runtime = SimpleNamespace(MODEL_REVISION='fixture', norm_ref=lambda text: text)
            torch = SimpleNamespace(manual_seed=Mock(), cuda=SimpleNamespace(manual_seed_all=Mock()))
            cache = {}
            request['project_root'] = str(root)
            with patch.object(module, '_load_runtime', return_value=(runtime, model, {}, torch, {})) as load:
                request['output_path'] = str(root / 'retained1.wav')
                progress = io.StringIO()
                with redirect_stdout(progress):
                    first = module.generate(request, runtime_cache=cache)
                self.assertEqual([json.loads(line)['stage'] for line in progress.getvalue().splitlines()],
                                 ['loading', 'generating', 'saving'])
                request.update(output_path=str(root / 'retained2.wav'), speed=0.9, seed=1234)
                progress = io.StringIO()
                with redirect_stdout(progress):
                    second = module.generate(request, runtime_cache=cache)
                self.assertEqual([json.loads(line)['stage'] for line in progress.getvalue().splitlines()],
                                 ['generating', 'saving'])
                self.assertEqual(load.call_count, 1)
                self.assertFalse(first['resident_model_reused'])
                self.assertTrue(second['resident_model_reused'])
                self.assertEqual(model.generate.call_args.kwargs['speed'], 0.9)
                torch.manual_seed.assert_called_with(1234)
                self.assertEqual(model.create_voice_clone_prompt.call_count, 3)
                reference.write_bytes(b'changed reference')
                with self.assertRaises(ValueError):
                    module.generate(request, runtime_cache=cache)
                self.assertEqual(model.generate.call_count, 3)

    def test_cutscene_reference_must_equal_exact_selected_native_frames_and_base_gxt_is_pinned(self):
        import soundfile as sf
        path = Path(__file__).resolve().parents[1] / 'tools' / 'omnivoice_tts.py'
        spec = importlib.util.spec_from_file_location('omnivoice_worker_cutscene_fixture', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'Track_001.wav'
            gxt = root / 'american.gxt'
            reference = root / 'selected.wav'
            output = root / 'take.wav'
            samples = module.np.linspace(-0.2, 0.2, 2400, dtype=module.np.float32)
            sf.write(source, samples, 24000, subtype='FLOAT')
            sf.write(reference, samples[500:1700], 24000, subtype='FLOAT')
            gxt.write_bytes(b'original English GXT source')
            request = {
                'project_root': str(root), 'reference_path': str(reference),
                'reference_sha256': hashlib.sha256(reference.read_bytes()).hexdigest(),
                'reference_text': 'Original line.', 'target_text': 'Linha em português.',
                'spoken_target_text': 'Linha em português.', 'output_path': str(output),
                'language': 'pt', 'speed': 1.0, 'duration': None, 'seed': 123,
                'window_seconds': 0.05, 'postprocess_output': False,
                'provenance': {
                    'source_kind': 'verified_native_cutscene_window', 'source_path': str(source),
                    'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                    'reference_start_frame': 500, 'reference_end_frame': 1700,
                    'source_sample_rate': 24000, 'gxt_stable_id': 'INTRO1:ABCD1234',
                    'gxt_source_path': str(gxt),
                    'gxt_source_sha256': hashlib.sha256(gxt.read_bytes()).hexdigest(),
                    'base_english_gxt_path': str(gxt),
                    'base_english_gxt_sha256': hashlib.sha256(gxt.read_bytes()).hexdigest(),
                    'base_english_text': 'Original line.', 'base_english_gxt_id': 'INTRO1:ABCD1234',
                },
            }
            model = SimpleNamespace(
                create_voice_clone_prompt=Mock(return_value='prompt'),
                generate=Mock(return_value=[module.np.full(600, 0.05, dtype=module.np.float32)]),
            )
            result = module.generate(request, model=model, model_revision='fixture')
            self.assertEqual(result['status'], 'UNREVIEWED_WITHIN_WINDOW')
            self.assertTrue(Path(result['output_path']).is_file())
            from studio.audio.cutscene_studio import import_replacement
            imported = import_replacement(output)
            self.assertEqual(imported['sample_rate'], 24000)
            self.assertEqual(imported['bits'], 24)
            altered_gxt = root / 'other.gxt'
            altered_gxt.write_bytes(b'different GXT')
            request['output_path'] = str(root / 'altered.wav')
            request['provenance']['gxt_source_path'] = str(altered_gxt)
            with self.assertRaisesRegex(ValueError, 'GXT inglês'):
                module.generate(request, model=model, model_revision='fixture')

    def test_all_silence_is_preserved_in_quarantine_and_never_reported_as_take(self):
        path = Path(__file__).resolve().parents[1] / 'tools' / 'omnivoice_tts.py'
        spec = importlib.util.spec_from_file_location('omnivoice_worker_silence_fixture', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            import soundfile as sf
            reference = root / 'ref.wav'
            source = root / 'Track_001.wav'
            gxt = root / 'american.gxt'
            sf.write(source, module.np.zeros(100, dtype=module.np.float32), 24000, subtype='FLOAT')
            sf.write(reference, module.np.zeros(10, dtype=module.np.float32), 24000, subtype='FLOAT')
            gxt.write_bytes(b'original english gxt')
            output = root / 'take.wav'
            request = {
                'reference_path': str(reference), 'reference_sha256': hashlib.sha256(reference.read_bytes()).hexdigest(),
                'reference_text': 'Original English.', 'target_text': 'Texto PT.', 'spoken_target_text': 'Texto PT.',
                'output_path': str(output), 'language': 'pt', 'speed': 1.0, 'duration': None,
                'seed': 123, 'window_seconds': 2.0, 'postprocess_output': False,
                'provenance': {'source_kind': 'verified_native_cutscene_window',
                    'source_path': str(source), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                    'reference_start_frame': 0, 'reference_end_frame': 10, 'source_sample_rate': 24000,
                    'gxt_stable_id': 'INTRO1:ABCD1234', 'gxt_source_path': str(gxt),
                    'gxt_source_sha256': hashlib.sha256(gxt.read_bytes()).hexdigest(),
                    'base_english_gxt_path': str(gxt),
                    'base_english_gxt_sha256': hashlib.sha256(gxt.read_bytes()).hexdigest(),
                    'base_english_text': 'Original English.', 'base_english_gxt_id': 'INTRO1:ABCD1234'},
            }
            model = SimpleNamespace(create_voice_clone_prompt=Mock(return_value='prompt'),
                generate=Mock(return_value=[module.np.zeros(24000, dtype=module.np.float32)]))
            result = module.generate(request, model=model, model_revision='fixture')
            self.assertEqual(result['status'], 'QUARANTINED_SILENCE')
            self.assertEqual(result['rms'], 0)
            self.assertEqual(result['clipping_ratio'], 0)
            self.assertTrue(Path(result['output_path']).is_file())

    def test_over_window_fake_take_is_reported_for_manual_review(self):
        path = Path(__file__).resolve().parents[1] / 'tools' / 'omnivoice_tts.py'
        spec = importlib.util.spec_from_file_location('omnivoice_worker_quarantine_fixture', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reference = root / 'cache' / 'export' / 'SCRIPT' / 'Bank_001' / 'sound_001.wav'
            reference.parent.mkdir(parents=True)
            reference.write_bytes(b'reference')
            archive = root / 'original.pak'
            archive.write_bytes(b'archive')
            base_gxt = root / 'american.gxt'
            base_gxt.write_bytes(b'original GXT')
            output = root / 'run' / 'INTRO1' / 'take.wav'
            request = {
                'reference_path': str(reference),
                'reference_sha256': hashlib.sha256(reference.read_bytes()).hexdigest(),
                'reference_text': 'Original English.', 'target_text': 'Texto PT.', 'spoken_target_text': 'Texto PT.',
                'output_path': str(output), 'language': 'pt', 'speed': 1.0,
                'duration': 12.5, 'seed': 20261009, 'window_seconds': 0.5,
                'mission': 'INTRO1', 'jamcrc': 'DEFE0050',
                'num_step': 32, 'postprocess_output': False,
                'provenance': {
                    'source_kind': 'original_game_saat_cache',
                    'source_path': str(reference),
                    'source_sha256': hashlib.sha256(reference.read_bytes()).hexdigest(),
                    'coordinates': ['SCRIPT', 1, 1], 'cache_root': str(root / 'cache'),
                    'archive_path': str(archive), 'archive_stat': [archive.stat().st_size, archive.stat().st_mtime_ns],
                    'base_english_gxt_path': str(base_gxt),
                    'base_english_gxt_sha256': hashlib.sha256(base_gxt.read_bytes()).hexdigest(),
                    'base_english_gxt_id': 'INTRO1:DEFE0050', 'base_english_text': 'Original English.',
                },
            }
            model = SimpleNamespace(
                create_voice_clone_prompt=Mock(return_value='prompt'),
                generate=Mock(return_value=[module.np.full(300000, 0.1, dtype=module.np.float32)]),
            )
            result = module.generate(request, model=model, model_revision='fixture')
            self.assertEqual(model.generate.call_args.kwargs['duration'], 12.5)
            self.assertEqual(result['duration_seconds'], 12.5)
            import soundfile as sf
            self.assertEqual(sf.info(result['output_path']).frames, 300000)
            self.assertEqual(result['speech_frames_cut'], 0)
            self.assertEqual(result['status'], 'QUARANTINED_DURATION')
            report = json.loads((output.parent / 'redub_relatorio.json').read_text(encoding='utf-8'))
            self.assertEqual(report[0]['arquivo_audio'], 'quarantine/take.wav')
            self.assertEqual(report[0]['omnivoice_receipt'], 'quarantine/take.omnivoice.json')
            self.assertTrue(Path(result['output_path']).is_file())
            request['output_path'] = str(root / 'long-within-window.wav')
            request['window_seconds'] = 20.0
            result = module.generate(request, model=model, model_revision='fixture')
            self.assertEqual(result['status'], 'UNREVIEWED_WITHIN_WINDOW')
            self.assertEqual(sf.info(result['output_path']).frames, 300000)


if __name__ == '__main__':
    unittest.main()
