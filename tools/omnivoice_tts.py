"""One-take local OmniVoice worker; no downloads, time stretch, or speech cuts."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf

_NONVERBAL_MARKER = re.compile(
    r'^\s*[\(\[]\s*(?:SCREAM|SCREAMING|SIGH|SIGHING|GASP|GASPING|LAUGH|LAUGHTER|'
    r'GROAN|GRUNT|CRY|COUGH|BREATHING|SILENCE|MUSIC|SOUND\s+EFFECT|SFX|'
    r'UNINTELLIGIBLE|INAUDIBLE)\s*[\)\]]\s*$', re.IGNORECASE)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _matches_stat(path_value, expected):
    if not path_value or not expected:
        return False
    stat = Path(path_value).stat()
    return [stat.st_size, stat.st_mtime_ns] == list(expected)


GPU_GUARD_DEFAULT = {'enabled': True, 'min_free_mib': 5120, 'max_utilization_percent': 10}


def _gpu_guard(request):
    guard = dict(GPU_GUARD_DEFAULT)
    given = (request or {}).get('gpu_guard')
    if isinstance(given, dict):
        guard.update({k: given[k] for k in guard if k in given})
    return guard


def _load_runtime(project_root, gpu_guard=None):
    guard = dict(GPU_GUARD_DEFAULT, **(gpu_guard or {}))
    runner = Path(project_root) / 'deliverables' / 'omnivoice_opening_pacing_v2' / 'run_omnivoice_opening_pacing_v2.py'
    if not runner.is_file():
        raise RuntimeError('Runner OmniVoice local validado não encontrado; sem download automático.')
    spec = importlib.util.spec_from_file_location('omnivoice_pinned_runtime', runner)
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    if not runtime.MODEL_SOURCE.is_dir():
        raise RuntimeError('Source OmniVoice cacheado ausente.')
    model_dir = runtime.MODEL_CACHE / f'models--k2-fsa--OmniVoice/snapshots/{runtime.MODEL_REVISION}'
    if not model_dir.is_dir():
        raise RuntimeError('Snapshot OmniVoice cacheado ausente; nenhum modelo será baixado.')
    pins = runtime.runtime_pins()
    if guard['enabled']:
        gpu = runtime.gpu_status()
        if (gpu['memory_free_mib'] < guard['min_free_mib'] or
                gpu['utilization_percent'] > guard['max_utilization_percent']):
            raise RuntimeError('GPU fora do limite seguro da canary (mínimo %s MiB livres e até %s%% de uso). '
                               'Desative a trava de GPU em Ajustes se a leitura estiver errada.' % (
                                   guard['min_free_mib'], guard['max_utilization_percent']))
    else:
        print('[AVISO] Trava de GPU desativada; seguindo sem checar uso/memória livre.', flush=True)
        try:
            gpu = runtime.gpu_status()
        except Exception:  # só registro no recibo; sem a trava, uma leitura ruim não pode bloquear
            gpu = None
    sys.path.insert(0, str(runtime.MODEL_SOURCE))
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    import torch
    from omnivoice import OmniVoice
    torch.cuda.set_device(0)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    model = OmniVoice.from_pretrained(str(model_dir), device_map='cuda:0', dtype=torch.float16,
                                      load_asr=False, local_files_only=True)
    return runtime, model, gpu, torch, pins


def _progress(stage, message):
    """Flush real lifecycle milestones for the editor and resident transport."""
    print(json.dumps({'event': 'stage', 'stage': stage, 'message': message},
                     ensure_ascii=False), flush=True)


def generate(request, model=None, model_revision=None, gpu_before=None, runtime_cache=None):
    """Run a single request. ``model`` is injectable for deterministic adapter tests."""
    if request.get('schema') not in (None, 'omnivoice-single-take/1'):
        raise ValueError('Schema de pedido OmniVoice incompatível.')
    reference = Path(request['reference_path']).resolve()
    output = Path(request['output_path']).resolve()
    expected = request.get('reference_sha256')
    if not reference.is_file() or not expected or sha256(reference) != expected:
        raise ValueError('WAV de referência original mudou desde a seleção.')
    source_path = (request.get('provenance') or {}).get('source_path')
    source_sha = (request.get('provenance') or {}).get('source_sha256')
    source_kind = (request.get('provenance') or {}).get('source_kind')
    if source_kind not in {'original_game_saat_cache', 'accepted_original_cutscene_utterance',
                           'verified_native_cutscene_window', 'verified_npc_catalog_original'}:
        raise ValueError('A fonte da referência não foi identificada como áudio original validado.')
    if source_path and (not Path(source_path).is_file() or sha256(source_path) != source_sha):
        raise ValueError('Faixa original mudou desde a seleção da referência.')
    if not source_path or not source_sha:
        raise ValueError('Caminho e hash da faixa original são obrigatórios.')
    provenance = request.get('provenance') or {}
    npc = source_kind == 'verified_npc_catalog_original'
    if npc:
        # CLI execution starts with tools/ on sys.path; import the shared strict adapter.
        app_root = str(Path(__file__).resolve().parents[1])
        if app_root not in sys.path:
            sys.path.insert(0, app_root)
        from studio.tts.omnivoice import validate_npc_provenance
        if request.get('scope') != 'NPC' or request.get('mission') or request.get('jamcrc'):
            raise ValueError('Pedido NPC deve usar apenas identidade física.')
        npc_row = validate_npc_provenance(provenance, request['reference_text'], request.get('physical_id'), request['project_root'])
        if request.get('window_seconds') != float(npc_row['duration_seconds']):
            raise ValueError('Janela NPC difere da duração original verificada.')
        if reference != Path(source_path).resolve():
            raise ValueError('Referência NPC deve ser o WAV original exato do slot.')
    target_gxt = provenance.get('target_gxt_path')
    if target_gxt and (not Path(target_gxt).is_file() or
                       sha256(target_gxt) != provenance.get('target_gxt_sha256')):
        raise ValueError('GXT alvo mudou desde a criação do pedido de voz.')
    english_gxt = provenance.get('base_english_gxt_path')
    if not npc and (not english_gxt or not Path(english_gxt).is_file() or
            sha256(english_gxt) != provenance.get('base_english_gxt_sha256') or
            not provenance.get('base_english_gxt_id') or not provenance.get('base_english_text')):
        raise ValueError('O pedido veio de uma janela do editor desatualizada para OmniVoice; feche e reabra o GXT Editor antes de gerar.')
    if source_kind == 'original_game_saat_cache':
        coordinates = provenance.get('coordinates') or []
        if len(coordinates) != 3:
            raise ValueError('Coordenadas PAK/banco/slot originais ausentes.')
        if any(isinstance(value, bool) or not isinstance(value, int) for value in coordinates[1:]):
            raise ValueError('Coordenadas PAK/banco/slot não são inteiras nativas.')
        pak, bank, sound = str(coordinates[0]).upper(), coordinates[1], coordinates[2]
        expected_file = Path(provenance.get('cache_root', '')) / 'export' / pak / f'Bank_{bank:03d}' / f'sound_{sound:03d}.wav'
        if Path(source_path).resolve() != expected_file.resolve() or reference != Path(source_path).resolve():
            raise ValueError('Referência não corresponde ao WAV exato do slot base informado.')
        if not _matches_stat(provenance.get('archive_path'), provenance.get('archive_stat')):
            raise ValueError('Arquivo de áudio base mudou desde a resolução SAAT.')
        if provenance.get('lookup_path') and not _matches_stat(provenance['lookup_path'], provenance.get('lookup_stat')):
            raise ValueError('BankLkup base mudou desde a resolução SAAT.')
    if provenance.get('source_audit_path'):
        if sha256(provenance['source_audit_path']) != provenance.get('source_audit_sha256'):
            raise ValueError('Auditoria nativa SCRIPT mudou desde a resolução do slot.')
        audit = json.loads(Path(provenance['source_audit_path']).read_text(encoding='utf-8'))
        if audit.get('cache_signature_matches') is not True or audit.get('source_unchanged') is not True:
            raise ValueError('Auditoria nativa SCRIPT não confirma cache original atual.')
        stable_id = str(provenance.get('stable_audio_id') or '').upper()
        sampled = next((row for row in audit.get('samples', []) if str(row.get('id', '')).upper() == stable_id), None)
        if provenance.get('source_audit_slot_sha256') and (
                not sampled or sampled.get('native_wav_sha256') != provenance['source_audit_slot_sha256'] or
                sampled.get('native_wav_sha256') != sha256(reference)):
            raise ValueError('Hash do slot base diverge da auditoria nativa SCRIPT.')
    if source_kind == 'accepted_original_cutscene_utterance':
        allowed = (Path(request['project_root']).resolve() /
                   'deliverables/omnivoice_cj_utterance_reference_test_v1/references').resolve()
        try:
            reference.relative_to(allowed)
        except ValueError:
            raise ValueError('A referência CJ não está no conjunto de WAVs originais alinhados aceitos.')
        report_path = Path(provenance.get('experiment_input_path', '')).resolve()
        if not report_path.is_file() or sha256(report_path) != provenance.get('experiment_input_sha256'):
            raise ValueError('Relatório da referência CJ original mudou.')
        if provenance.get('utterance_id') != 'Track_078:04' or provenance.get('reference_relation') != 'SAME_ORIGINAL_UTTERANCE':
            raise ValueError('A amostra CJ precisa ser a fala original aceita Track_078:04.')
        payload = json.loads(report_path.read_text(encoding='utf-8'))
        row = next((item for item in payload.get('records', [])
                    if item.get('id') == 'Track_078:04'), None)
        if (not row or Path(row.get('reference_path', '')).resolve() != reference or
                row.get('reference_sha256') != expected or
                row.get('reference_text', '').strip() != request['reference_text'].strip() or
                Path(row.get('reference_source', '')).resolve() != Path(source_path).resolve() or
                row.get('reference_source_sha256') != source_sha):
            raise ValueError('Relatório CJ diverge do WAV, da transcrição ou da faixa original fixados.')
        receipt_path = report_path.parent / 'Track_078_04_aligned_receipt.json'
        if not receipt_path.is_file():
            raise ValueError('Recibo da geração CJ aceita ausente.')
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        if receipt.get('reference_sha256') != expected or receipt.get('reference_text', '').strip() != request['reference_text'].strip():
            raise ValueError('Hash ou transcrição da referência CJ não coincide com o recibo aceito.')
    if source_kind == 'verified_native_cutscene_window':
        start_frame, end_frame = provenance.get('reference_start_frame'), provenance.get('reference_end_frame')
        rate = provenance.get('source_sample_rate')
        if (not isinstance(start_frame, int) or not isinstance(end_frame, int) or
                start_frame < 0 or end_frame <= start_frame or not isinstance(rate, int) or rate <= 0 or
            not provenance.get('gxt_stable_id')):
            raise ValueError('Janela CUTSCENE alinhada ou vínculo GXT sem proveniência completa.')
        gxt_path = Path(provenance.get('gxt_source_path', '')).resolve()
        if (not gxt_path.is_file() or not provenance.get('gxt_source_sha256') or
                sha256(gxt_path) != provenance.get('gxt_source_sha256')):
            raise ValueError('GXT inglês base mudou desde a seleção do trecho CUTSCENE.')
        with sf.SoundFile(source_path) as source, sf.SoundFile(reference) as clip:
            if source.samplerate != rate or end_frame > len(source):
                raise ValueError('Formato ou limites da faixa CUTSCENE mudaram.')
            if clip.samplerate != rate or len(clip) != end_frame - start_frame:
                raise ValueError('A referência não tem os limites exatos do trecho selecionado.')
            source.seek(start_frame)
            source_samples = source.read(end_frame - start_frame, dtype='float32', always_2d=True)
            clip_samples = clip.read(dtype='float32', always_2d=True)
            if source_samples.shape != clip_samples.shape or not np.array_equal(source_samples, clip_samples):
                raise ValueError('WAV de referência difere dos frames selecionados no original CUTSCENE.')
    # The immutable request pins the exact native WAV and its cache/source signature.
    # Do not hash a multi-hundred-megabyte archive per utterance; validate its recorded
    # stat/signature instead, then re-hash the exact source and reference WAVs above.
    if request.get('language') != 'pt' or request.get('postprocess_output') is not False:
        raise ValueError('Pedido precisa usar idioma pt e saída sem pós-processamento.')
    if (not str(request.get('reference_text') or '').strip() or
            not str(request.get('target_text') or '').strip() or
            not str(request.get('spoken_target_text') or '').strip()):
        raise ValueError('Transcrição de referência e texto-alvo são obrigatórios.')
    if _NONVERBAL_MARKER.fullmatch(request['reference_text'].strip()):
        raise ValueError('Referência é um marcador não verbal; escolha uma fala original do mesmo personagem.')
    if _NONVERBAL_MARKER.fullmatch(request['spoken_target_text'].strip()):
        raise ValueError('Alvo é não verbal; OmniVoice gera falas, não efeitos vocais.')
    if output.exists():
        raise ValueError('A saída já existe; o worker não sobrescreve takes.')
    speed = float(request['speed'])
    if not math.isfinite(speed) or not 0.5 <= speed <= 2.0:
        raise ValueError('Speed OmniVoice inválido.')
    window = request.get('window_seconds')
    if window is not None and (not math.isfinite(float(window)) or float(window) <= 0):
        raise ValueError('Janela de timing inválida.')
    duration_parameter = request.get('duration')
    if duration_parameter is not None and (not math.isfinite(float(duration_parameter)) or float(duration_parameter) <= 0):
        raise ValueError('Duration nativo OmniVoice inválido.')
    runtime = None
    torch_module = None
    runtime_pins = None
    reused = False
    if model is None:
        project_key = str(Path(request['project_root']).resolve())
        if runtime_cache is not None and runtime_cache.get('bundle') is not None:
            if runtime_cache['project_key'] != project_key:
                raise ValueError('Reinicie o modelo ao mudar de projeto.')
            runtime, model, gpu_before, torch_module, runtime_pins = runtime_cache['bundle']
            reused = True
        else:
            _progress('loading', 'Carregando modelo OmniVoice…')
            bundle = _load_runtime(request['project_root'], _gpu_guard(request))
            runtime, model, gpu_before, torch_module, runtime_pins = bundle
            if runtime_cache is not None:
                runtime_cache.update(project_key=project_key, bundle=bundle)
        model_revision = runtime.MODEL_REVISION
    seed = request.get('seed')
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1:
        raise ValueError('Seed OmniVoice inválido.')
    if torch_module is not None:
        torch_module.manual_seed(seed)
        torch_module.cuda.manual_seed_all(seed)
    ref_text = request['reference_text'].strip()
    if runtime is not None:
        ref_text = runtime.norm_ref(ref_text)
    _progress('generating', 'Preparando a voz e gerando áudio…')
    prompt = model.create_voice_clone_prompt(ref_audio=str(reference), ref_text=ref_text,
                                             preprocess_prompt=False)
    audio = model.generate(
        text=request['spoken_target_text'].strip(), language='pt', voice_clone_prompt=prompt,
        speed=speed, duration=duration_parameter,
        num_step=int(request.get('num_step', 32)),
        normalize_text=False, preprocess_prompt=False, postprocess_output=False,
        pad_duration=0.0, fade_duration=0.0,
    )[0]
    if hasattr(audio, 'detach'):
        audio = audio.detach().float().cpu().numpy()
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim != 1 or not len(audio) or not np.isfinite(audio).all():
        raise ValueError('OmniVoice retornou áudio vazio, inválido ou fora do formato mono esperado.')
    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
    peak = float(np.max(np.abs(audio)))
    clipping_ratio = float(np.count_nonzero(np.abs(audio) >= 0.999) / audio.size)
    silent = not np.any(audio)
    _progress('saving', 'Guardando o take e seu relatório…')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as stream:
        sf.write(stream, audio, 24000, subtype='PCM_24')
    duration = len(audio) / 24000
    quarantined_duration = window is not None and duration > float(window) + (1 / 24000)
    quarantined = quarantined_duration or silent
    if quarantined:
        quarantine = output.parent / 'quarantine'
        quarantine.mkdir(parents=True, exist_ok=True)
        quarantined_path = quarantine / output.name
        if quarantined_path.exists():
            raise ValueError('Take de quarentena já existe; preservado sem sobrescrita.')
        output.rename(quarantined_path)
        output = quarantined_path
    result = {
        'schema': 'omnivoice-single-take-result/1', 'engine': 'OmniVoice',
        'model_revision': model_revision, 'language': 'pt',
        'reference_path': str(reference), 'reference_sha256': expected,
        'reference_text': request['reference_text'].strip(), 'reference_text_in_prompt': ref_text,
        'reference_prompt_tokens_sha256': getattr(runtime, 'sample_condition_sha', lambda _p: None)(prompt)
            if runtime else None,
        'target_text': request['target_text'].strip(),
        'spoken_target_text': request['spoken_target_text'].strip(), 'output_path': str(output),
        'mission': request.get('mission'), 'jamcrc': request.get('jamcrc'),
        'scope': request.get('scope', 'GXT'), 'physical_id': request.get('physical_id'),
        'target_gxt_id': ('%s:%s' % (request['mission'], str(request['jamcrc']).upper())
                          if request.get('mission') and request.get('jamcrc') else None),
        'output_sha256': sha256(output), 'sample_rate': 24000, 'duration_seconds': duration,
        'window_seconds': float(window) if window is not None else None,
        'status': ('QUARANTINED_SILENCE' if silent else 'QUARANTINED_DURATION') if quarantined else 'UNREVIEWED_WITHIN_WINDOW',
        'speed': float(request['speed']), 'duration_parameter': request.get('duration'), 'seed': seed,
        'rms': rms, 'peak': peak, 'clipping_ratio': clipping_ratio,
        'postprocess_output': False, 'post_synthesis_time_stretch': False,
        'speech_frames_cut': 0, 'provenance': request.get('provenance') or {},
        'gpu_before': gpu_before, 'runtime_pins': runtime_pins,
        'production_approved': False, 'installed': False,
        'resident_model_reused': reused, 'worker_pid': os.getpid(),
        'created_utc': datetime.now(timezone.utc).isoformat(),
    }
    receipt = output.with_suffix('.omnivoice.json')
    with receipt.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    # A duration mismatch is reviewable; silence remains a failed take.
    report_root = output.parent.parent if quarantined else output.parent
    if request.get('mission') and request.get('jamcrc'):
        report = report_root / 'redub_relatorio.json'
        rows = [] if silent else [{
            'missao': request['mission'], 'jamcrc': request['jamcrc'],
            'arquivo_audio': output.relative_to(report_root).as_posix(),
            'omnivoice_receipt': receipt.relative_to(report_root).as_posix(),
        }]
        with report.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--request')
    mode.add_argument('--serve', action='store_true')
    args = parser.parse_args(argv)
    if args.serve:
        import traceback
        cache = {}
        for line in sys.stdin:
            message = {}
            try:
                message = json.loads(line)
                request = json.loads(Path(message['request_path']).read_text(encoding='utf-8'))
                result = generate(request, runtime_cache=cache)
                print(json.dumps({key: result[key] for key in
                    ('status', 'output_path', 'duration_seconds', 'window_seconds', 'output_sha256')}, ensure_ascii=False), flush=True)
                reply = {'id': message['id'], 'returncode': 2 if result['status'] == 'QUARANTINED_SILENCE' else 0}
            except Exception as exc:
                traceback.print_exc()
                reply = {'id': message.get('id'), 'returncode': 1, 'error': str(exc)}
            print('__OMNI_RESULT__' + json.dumps(reply, ensure_ascii=False), flush=True)
        return
    request_path = Path(args.request).resolve()
    request = json.loads(request_path.read_text(encoding='utf-8'))
    result = generate(request)
    print(json.dumps({key: result[key] for key in
                      ('status', 'output_path', 'duration_seconds', 'window_seconds', 'output_sha256')},
                     ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
