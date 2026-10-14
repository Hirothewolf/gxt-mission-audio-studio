"""Adapters to the classic stores, native routing and pinned OmniVoice CLI."""
import uuid
import threading
import hashlib
from pathlib import Path

from studio import config
from studio.audio.resolver import BaseAudioResolver
from studio.audio.source_routing import is_stream_record, audio_route, output_directory
from studio.audio.voice_identity import VoiceIdentityStore
from studio.gxt.model import Gxt
from studio.gxt.codec import decode_text
from studio.projects.review import ReviewStore
from studio.projects.bulk_audio import collect_ai_rows, install_rows
from studio.saat.audio import GameAudio
from studio.script.mapping import carregar_script_cache, consultar_script_info, ref_do_registro
from studio.tts.jobs import JobQueue
from .playback import QtAudioPlayer


class QtContext:
    def __init__(self, document, project_root=None, parent=None):
        self.document = document
        self.project_root = Path(project_root or config.PROJ_ROOT).resolve()
        self.game_audio = GameAudio(config.CFG, config.cfg_save,
            self.project_root / '.cache/saat', self.project_root / 'backups/modloader',
            self.project_root / 'workspace/modloader_manifest.json')
        self.review_store = ReviewStore(self.project_root)
        self.identity_store = VoiceIdentityStore(self.project_root)
        self.player = QtAudioPlayer(parent)
        self.jobs = JobQueue()
        self.jobs.set_keep_model_loaded(bool(config.CFG.get('omnivoice_keep_model_loaded', False)))
        self.script_db = carregar_script_cache()
        self.base_audio = BaseAudioResolver()
        self._base = None
        self._base_signature = None
        self._write_lock = threading.Lock()
        self._resolver_ready = False
        self.filter_installation_evidence = {}

    def initialize_resolver(self):
        roots = []
        cache = self.project_root / '.cache/saat'
        if cache.is_dir():
            roots.extend(sub / 'export/SCRIPT' for sub in cache.iterdir() if (sub / 'export/SCRIPT').is_dir())
        for name in ('audio_original', 'original_audio', 'audio_extracted', 'extracted_audio'):
            path = self.project_root / name
            if path.is_dir():
                roots.append(path)
        events = [self.project_root / 'data/AudioEvents.txt', self.project_root / 'AudioEvents.txt']
        if self.game_audio.game_dir:
            events.append(self.game_audio.game_dir / 'data/AudioEvents.txt')
        events = [path for path in events if path.is_file()]
        if not self.base_audio.load_cache(roots, events):
            self.base_audio.scan(roots, events)
        self._resolver_ready = True

    def current_text(self, record):
        gxt = self.document.gxt
        if gxt and record['table'] in gxt.t and record['hash'] in gxt.t[record['table']]['cur']:
            return decode_text(gxt.t[record['table']]['cur'][record['hash']])
        return record.get('text', '')

    def info(self, record):
        info = dict(record.get('info') or consultar_script_info(
            self.script_db, record['table'], record['hash'], record.get('name')) or {})
        if not info:
            return info
        line_id = record.get('line_id') or '%s|%08x' % (record['table'], record['hash'])
        if not hasattr(self, '_audio_record_info'):
            self._audio_record_info = {}
        self._audio_record_info[(str(getattr(self.document, 'path', '')), line_id)] = dict(info)
        manual = self.review_store.character(line_id)
        if manual:
            info.update(speaker_identity=manual, speaker_identity_status='MANUAL')
        elif info.get('speaker_identity_status') != 'VERIFIED':
            original = self.game_audio.original_para(info) if not is_stream_record(info) else None
            voice = self.identity_store.lookup(info, original)
            if voice:
                info.update(speaker_identity=voice.get('identity'), speaker_identity_status=voice.get('status'))
        return info

    def original_path(self, record):
        if record.get('reference_override'):
            return self.manual_reference(record['reference_override'])[0]
        info = self.info(record)
        if not info:
            return None
        if is_stream_record(info):
            from studio.tts.stream_reference import resolve_stream_reference
            ref = resolve_stream_reference(info, self.project_root)
            return Path(ref.get('audition_path') or ref['path'])
        path = self.game_audio.original_para(info)
        if not path and hasattr(self.game_audio, 'ref_para'):
            from .audio_sync import extract_sfx_slot
            path = extract_sfx_slot(info, self.project_root, self.game_audio)
        if not path:
            if not self._resolver_ready:
                self.initialize_resolver()
            path, method, confidence = self.base_audio.resolve(record['table'], record['hash'], record.get('name'), info)
        return Path(path) if path else None

    def installed_path(self, record):
        info = self.info(record)
        if not info:
            return None
        from .audio_sync import installed_line_evidence, extract_sfx_slot, extract_stream_line
        mod = self.selected_mod()
        evidence = installed_line_evidence(info, self.project_root, self.game_audio, mod=mod)
        if evidence['path']:
            self.record_installed_evidence(record, info, evidence['path'])
            return evidence['path']
        if is_stream_record(info):
            path = extract_stream_line(info, self.project_root, self.game_audio, mod)
        elif evidence['state'] == 'needs_extract':
            path = extract_sfx_slot(info, self.project_root, self.game_audio, mod)
        else:
            path = None
        self.record_installed_evidence(record, info, path)
        return path

    def selected_mod(self):
        from .audio_sync import detect_mod_from_gxt
        path = getattr(self.document, 'path', None)
        return (detect_mod_from_gxt(path, self.game_audio) if path else None) or self.game_audio.mod_ativo()

    def audio_evidence(self, record, source='mod', take_path=None):
        """Presence query only; callers may run it in a worker for large proof hashes.

        state: available / needs_extract / not_found / stale / proveninstalled.
        verified establishes the physical source, never approval or current text.
        """
        from .audio_sync import installed_line_evidence, sfx_slot, cached_sfx_slot
        info = self.info(record)
        result = dict(state='not_found', status='unavailable', path=None, verified=False, matches_take=None)
        if source == 'take':
            line_id = record.get('line_id') or '%s|%08x' % (record['table'], record['hash'])
            path = self.approved_path(line_id, self.current_text(record))
            if path:
                result.update(state='available', status='verified', path=path, verified=True)
            return result
        if not info:
            return result
        if source == 'mod':
            return installed_line_evidence(info, self.project_root, self.game_audio, take_path, self.selected_mod())
        if source != 'original':
            raise ValueError('Fonte de áudio desconhecida: ' + source)
        if is_stream_record(info):
            route = audio_route(info)
            game = self.game_audio.game_dir
            if game and (game / 'audio/streams' / route['container']).is_file():
                result.update(state='needs_extract', status='embedded_unindexed')
            return result
        path = self.game_audio.original_para(info)
        if path and Path(path).is_file():
            result.update(state='available', status='verified', path=Path(path), verified=True)
        else:
            slot = sfx_slot(info, self.game_audio)
            if slot:
                path = cached_sfx_slot(slot, self.project_root)
                if path:
                    result.update(state='available', status='verified', path=path, verified=True)
                else:
                    result.update(state='needs_extract', status='embedded_unindexed')
        return result

    def record_installed_evidence(self, record, info, path):
        from .audio_sync import installed_line_evidence
        line_id = record.get('line_id') or '%s|%08x' % (record['table'], record['hash'])
        approved = self.approved_path(line_id, self.current_text(record))
        evidence = installed_line_evidence(info, self.project_root, self.game_audio, take_path=approved, mod=self.selected_mod())
        if not hasattr(self, 'filter_installation_evidence'):
            self.filter_installation_evidence = {}
        if evidence['matches_take'] is not None:
            self.filter_installation_evidence[line_id] = evidence['matches_take']
        else:
            self.filter_installation_evidence.pop(line_id, None)

    def approved_path(self, line_id, text):
        path = self.review_store.approved_path(line_id, text)
        if path:
            return path
        # Imported installed takes may have a proof without a local ReviewStore
        # candidate. Require the exact line, live install, text and source hash.
        from .audio_sync import cached_json, _ui_sha256
        try:
            table, key = line_id.split('|')
            record = dict(table=table, hash=int(key, 16), line_id=line_id, text=text)
            known = getattr(self, '_audio_record_info', {}).get(
                (str(getattr(getattr(self, 'document', None), 'path', '')), line_id))
            if known:
                record['info'] = known
            evidence = self.audio_evidence(record, 'mod')
            proof = evidence.get('proof', {})
            if evidence['state'] != 'proveninstalled':
                return None
            path = Path(proof.get('source_take_path', ''))
            path.resolve().relative_to(self.project_root)
            if not path.is_file() or not proof.get('source_take_sha256') or _ui_sha256(path) != proof['source_take_sha256']:
                return None
            authority = proof
            receipt = path.with_suffix('.omnivoice.json')
            if receipt.is_file():
                authority = cached_json(receipt)
            elif path.name == 'audio.wav' and (path.parent / 'receipt.json').is_file():
                receipt_data = cached_json(path.parent / 'receipt.json')
                stable_id = '%s:%08X' % (table, int(key, 16))
                if (receipt_data.get('gxt_id') != stable_id or
                        receipt_data.get('output_sha256') != proof['source_take_sha256']):
                    return None
                from studio.tts.omnivoice import spoken_text
                spoken = spoken_text(text)
                if (receipt_data.get('gxt_value_sha256') and receipt_data['gxt_value_sha256'] !=
                        hashlib.sha256(spoken.encode('utf-8')).hexdigest()):
                    return None
                if receipt_data.get('pt_target_exact') == spoken:
                    return path
                authority = receipt_data
            expected = authority.get('target_text_sha256') or authority.get('text_hash')
            exact_text = authority.get('target_text', authority.get('text', authority.get('pt_target_exact')))
            if expected == hashlib.sha256(text.encode('utf-8')).hexdigest() or exact_text == text:
                return path
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return None

    def chosen_path(self, line_id, text):
        pending = getattr(self, 'pending_audio', None)
        choice = pending.choices(self.document.path).get(line_id) if pending else None
        if choice and choice['record']['text'] == text:
            candidate = self.review_store.validate_candidate_source(line_id, choice['candidate_id'])
            if candidate['sha256'] != choice['sha256']:
                raise ValueError('O take escolhido foi alterado. Selecione outro take antes de ouvir ou salvar.')
            return self.review_store.resolve_path(candidate['path'])
        return self.approved_path(line_id, text)

    def manual_reference(self, manual):
        import soundfile as sf
        from studio.tts.omnivoice import sha256
        source = Path(manual['source_path'])
        if sha256(source) != manual['source_sha256']:
            raise ValueError('O original selecionado mudou desde a edição na timeline.')
        first, last = int(manual['start_frame']), int(manual['end_frame'])
        with sf.SoundFile(source) as audio:
            rate = audio.samplerate
            if first < 0 or last <= first or last > len(audio):
                raise ValueError('O trecho selecionado está fora da faixa original.')
            audio.seek(first)
            samples = audio.read(last-first, dtype='float32', always_2d=True)
        folder = self.project_root / '.cache/qt_manual_references'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ('%s_%s_%s.wav' % (manual['source_sha256'], first, last))
        if not path.is_file():
            temporary = path.with_name(uuid.uuid4().hex + '.wav')
            sf.write(temporary, samples, rate, subtype='FLOAT')
            temporary.replace(path)
        else:
            import numpy as np
            saved, saved_rate = sf.read(path, dtype='float32', always_2d=True)
            if saved_rate != rate or not np.array_equal(saved, samples):
                raise ValueError('O trecho de referência salvo foi alterado; selecione novamente a fonte original.')
        return path, first, last, rate

    def _native_generation_reference(self, info):
        """TTS worker preflight, retaining the strict native provenance guard.

        Audition's individual-slot cache is deliberately not a TTS authority.
        Only generation preparation may populate the full original SAAT cache.
        """
        from studio.tts.omnivoice import resolve_original_reference
        if not self.game_audio.original_para(info):
            ref = self.game_audio.ref_para(info)
            if not ref:
                raise ValueError('Falta o vínculo exato de áudio original desta fala.')
            result = self.game_audio.indexar(ref[0])
            if not result.get('ok'):
                raise ValueError('Não foi possível preparar a referência original: ' +
                                 result.get('motivo', 'extração falhou'))
        return resolve_original_reference(info, self.game_audio, self.project_root)

    def generation_request(self, record, text, speed=1.0, duration='livre', seed=None,
                           engine='OmniVoice', max_speedup=None, exaggeration=None):
        from studio.tts.omnivoice import build_command, resolve_original_reference, sha256, spoken_text
        from studio.tts.stream_reference import resolve_stream_reference
        from studio.tts.registry import select_engine
        from studio.audio.wav import wav_duracao
        selected_engine = select_engine(engine, root=self.project_root)
        info = self.info(record)
        if not info:
            raise ValueError('Esta chave ainda não tem um vínculo de áudio original. Reindexe os áudios do jogo.')
        if selected_engine.id != 'omnivoice':
            if is_stream_record(info):
                raise ValueError('Para um trecho de CUTSCENE/BEATS use OmniVoice, que recebe a referência exata. '
                    'Este adaptador não recebe referência por trecho.')
            import copy
            from studio.gxt.codec import encode_text
            run = self.project_root / 'workspace/studio_runs' / uuid.uuid4().hex
            run.mkdir(parents=True)
            snapshot = copy.deepcopy(self.document.gxt)
            encoded, invalid = encode_text(text)
            if invalid:
                raise ValueError('O texto contém caracteres incompatíveis com o GXT.')
            snapshot.t[record['table']]['cur'][record['hash']] = encoded
            snapshot_path = run / 'american.gxt'
            snapshot.save(snapshot_path)
            output = run / 'output'
            argv = selected_engine.build_command(record['table'], snapshot_path, output,
                jamcrc='%08X' % record['hash'], max_speedup=max_speedup, exaggeration=exaggeration)
            original = self.original_path(record)
            return dict(engine=selected_engine.name, argv=argv, run=run, output_dir=output / record['table'],
                request=dict(target_text=text, engine=selected_engine.name),
                window_seconds=wav_duracao(original) if original else None)
        base_path = self.game_audio.game_dir / 'text/american.gxt' if self.game_audio.game_dir else None
        if not base_path or not base_path.is_file():
            raise ValueError('Selecione a pasta do GTA com o american.gxt original.')
        signature = (str(base_path), base_path.stat().st_mtime_ns, base_path.stat().st_size)
        if signature != self._base_signature:
            self._base = Gxt(str(base_path))
            self._base_signature = signature
        table, key = record['table'], record['hash']
        english_full = decode_text(self._base.t.get(table, {}).get('cur', {}).get(key, b''))
        english = spoken_text(english_full)
        if not english:
            raise ValueError('Falta o texto original desta chave. Uma tradução não pode ser usada como transcrição da referência.')
        if record.get('reference_override') and is_stream_record(info):
            manual = record['reference_override']
            source = Path(manual['source_path'])
            path, first, last, rate = self.manual_reference(manual)
            reference = dict(path=str(path))
            ref_text = manual.get('transcript') or english
            window = (last-first) / rate
            provenance = dict(source_kind='verified_native_cutscene_window', source_path=str(source),
                source_sha256=manual['source_sha256'], reference_start_frame=first, reference_end_frame=last,
                source_sample_rate=rate, gxt_stable_id=info.get('gxt_stable_id') or '%s:%08X' % (table, key),
                reference_transcript_authority='GXT_KEY_FOR_MANUALLY_SELECTED_ORIGINAL_SPAN',
                reference_asr_verification='PENDING', manual_timeline_selection=True)
        elif is_stream_record(info):
            reference = resolve_stream_reference(info, self.project_root, english_full)
            ref_text = reference['reference_text']
            provenance = dict(reference['provenance'])
            window = reference['duration_seconds']
        else:
            reference = self._native_generation_reference(info)
            ref_text = english
            window = wav_duracao(reference['path'])
            provenance = dict(reference)
            provenance.update(source_path=reference['path'], source_sha256=reference['sha256'],
                source_text_en=english, reference_transcript_authority='NATIVE_GXT_CAPTION_NOT_BLIND_ASR',
                reference_asr_verification='PENDING')
        if not window:
            raise ValueError('Não foi possível medir a fala original. Extraia o banco correspondente novamente.')
        route = audio_route(info)
        run = self.project_root / 'workspace/studio_runs' / uuid.uuid4().hex
        output = output_directory(run, route, table) / ('%08X.wav' % key)
        target = self.document.path
        provenance.update(audio_route=route, target_stable_id=info.get('stable_audio_id'),
            target_speaker_identity=info.get('speaker_identity'), target_identity_status=info.get('speaker_identity_status', 'UNKNOWN'),
            gxt_source_path=str(base_path), gxt_source_sha256=sha256(base_path),
            target_text_source='current_GXT_editor_text', timing_source='exact_original_game_utterance_duration',
            timing_reference_path=reference['path'], timing_window_seconds=window,
            base_english_gxt_path=str(base_path), base_english_gxt_sha256=sha256(base_path),
            base_english_text=english_full, base_english_gxt_id='%s:%08X' % (table, key),
            target_gxt_path=str(target) if target else None,
            target_gxt_sha256=sha256(target) if target and target.is_file() else None,
            target_gxt_file_is_saved_snapshot=True,
            target_gxt_has_unsaved_edits=self.document.dirty,
            target_text_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest())
        engine = select_engine('OmniVoice', root=self.project_root)
        argv = build_command(self.project_root, reference['path'], ref_text, text, output,
            provenance=provenance, speed=speed, duration=duration, window_seconds=window,
            python_path=engine.python, script_path=engine.script, mission=table,
            jamcrc='%08X' % key, seed=seed)
        return dict(engine='OmniVoice', argv=argv, request=Path(argv[-1]), run=run, output=output,
                    receipt=output.with_suffix('.omnivoice.json'))

    def install(self, records, progress=None):
        contexts = []
        for record in records:
            item = dict(record)
            item['line_id'] = item.get('line_id') or '%s|%08x' % (item['table'], item['hash'])
            item['info'] = self.info(item)
            item['ref'] = ref_do_registro(item['info'])
            item['label'] = item.get('label') or item.get('name') or item['line_id']
            if self.current_text(item) != item['text']:
                raise ValueError('O texto mudou desde a seleção. Atualize a lista antes de aplicar o áudio.')
            contexts.append(item)
        with self._write_lock:
            rows = collect_ai_rows(self.review_store, contexts)
            return install_rows(self.review_store, rows, self.game_audio, self.game_audio.mod_ativo(), progress)

    def shutdown(self):
        self.player.stop()
        self.jobs.shutdown(wait=False)
