"""Strict, provenance-preserving OmniVoice adapter for one dialogue take."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
from pathlib import Path

from studio import config

_PAKS = {'FEET', 'GENRL', 'PAIN_A', 'SCRIPT', 'SPC_EA', 'SPC_FA', 'SPC_GA', 'SPC_NA', 'SPC_PA'}
_STABLE = re.compile(r'(?i)^(SCRIPT|GENRL|FEET|PAIN_A|SPC_EA|SPC_FA|SPC_GA|SPC_NA|SPC_PA):B(\d+):S(\d+)$')
_GTASA_TAG = re.compile(r'~[^~]*~')
_NONVERBAL_MARKER = re.compile(
    r'^\s*[\(\[]\s*(?:SCREAM|SCREAMING|SIGH|SIGHING|GASP|GASPING|LAUGH|LAUGHTER|'
    r'GROAN|GRUNT|CRY|COUGH|BREATHING|SILENCE|MUSIC|SOUND\s+EFFECT|SFX|'
    r'UNINTELLIGIBLE|INAUDIBLE)\s*[\)\]]\s*$', re.IGNORECASE)
_SLOT = re.compile(r'(?i)Bank[_ -]0*(\d+)[\\/]sound[_ -]0*(\d+)\.wav$')
_OMNI_RUNNER = Path('deliverables/omnivoice_opening_pacing_v2/run_omnivoice_opening_pacing_v2.py')
_CJ_REPORT = Path('reports/studio_rework_20261008/omnivoice_cj_utterance_reference_test_v1/aligned_comparison_input.json')
_CJ_REFERENCE_DIR = Path('deliverables/omnivoice_cj_utterance_reference_test_v1/references')
_NATIVE_SCRIPT_AUDIT = Path('reports/studio_rework_20261009/omnivoice_editor_native_audit/source_audit.json')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def spoken_text(text):
    """Remove GTA formatting tags from the speech input without editing GXT text."""
    return _GTASA_TAG.sub('', str(text or '')).strip()


def is_nonverbal_marker(text):
    return bool(_NONVERBAL_MARKER.fullmatch(spoken_text(text)))


def validate_npc_provenance(provenance, reference_text, physical_id, project_root):
    """Verify a physical SPC reference against the completed, pinned ASR catalog.

    ASR is draft evidence, never a GXT caption or a speaker identification.
    """
    from studio.audio.npc_catalog import load_verified_catalog
    root = Path(project_root).resolve()
    if provenance.get('source_kind') != 'verified_npc_catalog_original' or provenance.get('scope') != 'NPC':
        raise ValueError('Proveniência NPC física ausente.')
    if any(provenance.get(k) for k in ('gxt_stable_id', 'base_english_gxt_id', 'target_gxt_path')):
        raise ValueError('NPC físico não pode receber identidade GXT fictícia.')
    catalog = load_verified_catalog(root)
    if (Path(provenance.get('catalog_path', '')).resolve() != catalog.catalog_path.resolve() or
            provenance.get('catalog_sha256') != catalog.summary['catalog_sha256'] or
            provenance.get('physical_id') != physical_id):
        raise ValueError('Catálogo ou ID físico NPC mudou desde a seleção.')
    row = catalog.get(physical_id)
    if not row or row['scope'] == 'PLAYER_ROUTE_SEPARATE':
        raise ValueError('ID físico ausente do escopo NPC.')
    if row['status'] != 'ASR_DRAFT' or not row.get('reference_text', '').strip():
        raise ValueError('NPC sem fala transcrita não pode ser usado como referência TTS.')
    if reference_text != row['reference_text'].strip():
        raise ValueError('Transcrição de referência difere do ASR verificado do slot NPC.')
    source = Path(row['source_audio']).resolve()
    if (not source.is_relative_to(root) or
            Path(provenance.get('source_path', '')).resolve() != source or
            provenance.get('source_sha256') != row['source_sha256'] or
            not source.is_file() or sha256(source) != row['source_sha256']):
        raise ValueError('WAV original NPC ausente, alterado ou pertencente a outro slot.')
    if provenance.get('reference_text') != reference_text:
        raise ValueError('Transcrição NPC na proveniência está divergente.')
    return row


def coordinates_from_record(record):
    """Normalize every supplied PAK/slot representation and reject disagreement.

    Stable IDs and bank_index/sound_index use zero-based manifest coordinates.
    SAAT bank_num/sound_num and extracted WAV paths use one-based coordinates.
    """
    if not isinstance(record, dict):
        raise ValueError('Mapeamento SCRIPT ausente ou ambíguo.')
    pak_candidates = set()
    for key in ('pak', 'archive', 'package', 'sfx_archive', 'container'):
        value = str(record.get(key) or '').strip().upper()
        if value:
            pak_candidates.add(value)
    zero_based = []
    one_based = []
    for key in ('stable_audio_id', 'audio_id'):
        value = str(record.get(key) or '').strip()
        if value:
            match = _STABLE.fullmatch(value)
            if match:
                pak_candidates.add(match.group(1).upper())
                zero_based.append((int(match.group(2)), int(match.group(3)), key))
            elif key == 'stable_audio_id':
                raise ValueError('ID de áudio estável inválido; OmniVoice exige vínculo PAK/banco/slot explícito.')
    for b_key, s_key in (('bank_index', 'sound_index'), ('bank_num', 'sound_num')):
        b, s = record.get(b_key), record.get(s_key)
        if (b is None) != (s is None):
            raise ValueError('Mapeamento de banco/slot incompleto.')
        if b is not None:
            if isinstance(b, bool) or isinstance(s, bool):
                raise ValueError('Mapeamento de banco/slot inválido.')
            try:
                bank_value, sound_value = float(b), float(s)
                if not math.isfinite(bank_value) or not math.isfinite(sound_value) or not bank_value.is_integer() or not sound_value.is_integer():
                    raise ValueError
                pair = int(bank_value), int(sound_value)
            except (TypeError, ValueError):
                raise ValueError('Mapeamento de banco/slot inválido.')
            (zero_based if b_key.endswith('_index') else one_based).append((*pair, b_key))
    for key in ('source_wav_path', 'source_wav', 'wav', 'sound_path', 'path', 'file', 'rel'):
        value = str(record.get(key) or '').strip()
        if value:
            match = _SLOT.search(value.replace('/', '\\'))
            if match:
                one_based.append((int(match.group(1)), int(match.group(2)), key))
                parts = Path(value.replace('/', '\\')).parts
                for part in parts:
                    if part.upper() in _PAKS:
                        pak_candidates.add(part.upper())
                        break
    if len(pak_candidates) != 1 or next(iter(pak_candidates)) not in _PAKS:
        raise ValueError('Mapeamento de PAK ausente ou ambíguo.')
    if not zero_based and not one_based:
        raise ValueError('Mapeamento banco/slot ausente.')
    normalized = {(b, s) for b, s, _ in zero_based}
    normalized.update((b - 1, s - 1) for b, s, _ in one_based)
    if any(b < 0 or s < 0 for b, s in normalized) or len(normalized) != 1:
        detail = ', '.join(f'{key}={b}:{s}' for b, s, key in zero_based + one_based)
        raise ValueError('Representações de banco/slot em conflito: ' + detail)
    zero_bank, zero_sound = next(iter(normalized))
    return next(iter(pak_candidates)), zero_bank + 1, zero_sound + 1


def _file_stat(path_value):
    if not path_value:
        return None
    stat = Path(path_value).stat()
    return [stat.st_size, stat.st_mtime_ns]


def resolve_original_reference(record, game_audio, project_root=None):
    """Resolve the exact base-game cache WAV; never consult ModLoader or AutoSearch."""
    pak, bank, sound = coordinates_from_record(record)
    if game_audio is None or not callable(getattr(game_audio, '_cache_original_atual', None)):
        raise ValueError('Cache original SAAT indisponível; não use AutoSearch como referência OmniVoice.')
    if not game_audio._cache_original_atual(pak):
        raise ValueError(f'O cache original {pak} não está indexado/atual; indexe o áudio base do jogo.')
    path_value = (getattr(game_audio, 'indice', {}) or {}).get((pak, bank, sound))
    if not path_value:
        try:
            game_audio.carregar_indice()
        except (AttributeError, OSError, ValueError):
            pass
        path_value = (getattr(game_audio, 'indice', {}) or {}).get((pak, bank, sound))
    if not path_value:
        raise ValueError(f'Slot original {pak}:Bank_{bank:03d}/sound_{sound:03d}.wav ausente no cache base.')
    path = Path(path_value).resolve()
    cache = Path(game_audio.cache_dir()).resolve()
    expected_root = (cache / 'export' / pak).resolve()
    expected_path = expected_root / f'Bank_{bank:03d}' / f'sound_{sound:03d}.wav'
    try:
        relative = path.relative_to(expected_root)
    except ValueError:
        raise ValueError('A referência não está no cache original SAAT do jogo base.')
    if path != expected_path.resolve() or relative.parts != (f'Bank_{bank:03d}', f'sound_{sound:03d}.wav'):
        raise ValueError('Caminho de referência não corresponde exatamente ao banco/slot base mapeado.')
    if not path.is_file():
        raise ValueError('WAV original do slot ausente no cache SAAT.')
    archive_path = (game_audio._arquivo_pak_jogo(pak).resolve()
                    if callable(getattr(game_audio, '_arquivo_pak_jogo', None)) else None)
    lookup_path = (Path(game_audio.info.get('banklkup')).resolve()
                   if getattr(game_audio, 'info', {}).get('banklkup') else None)
    signature = game_audio._assinatura_fonte(pak) if callable(getattr(game_audio, '_assinatura_fonte', None)) else None
    result = {
        'path': str(path), 'sha256': sha256(path), 'coordinates': [pak, bank, sound],
        'source_kind': 'original_game_saat_cache', 'cache_root': str(cache),
        'archive_signature': signature,
        'archive_path': str(archive_path) if archive_path else None,
        'archive_stat': _file_stat(archive_path) if archive_path else None,
        'lookup_path': str(lookup_path) if lookup_path else None,
        'lookup_stat': _file_stat(lookup_path) if lookup_path else None,
    }
    if pak == 'SCRIPT':
        root = Path(project_root or config.get_project_root()).resolve()
        audit_path = root / _NATIVE_SCRIPT_AUDIT
        if audit_path.is_file():
            audit = json.loads(audit_path.read_text(encoding='utf-8'))
            if audit.get('cache_signature_matches') is not True or audit.get('source_unchanged') is not True:
                raise ValueError('Auditoria do áudio SCRIPT base está desatualizada ou não íntegra.')
            stable_id = str(record.get('stable_audio_id') or f'SCRIPT:B{bank - 1:03d}:S{sound - 1:03d}').upper()
            sampled = next((item for item in audit.get('samples', []) if str(item.get('id', '')).upper() == stable_id), None)
            if sampled:
                if Path(sampled.get('native_path', '')).resolve() != path or sampled.get('native_wav_sha256') != result['sha256']:
                    raise ValueError('Hash do slot original SCRIPT difere da auditoria nativa fixada.')
            result['source_audit_path'] = str(audit_path)
            result['source_audit_sha256'] = sha256(audit_path)
            result['native_source_sha256'] = audit.get('native_source_sha256')
            result['native_lookup_sha256'] = audit.get('lookup_sha256')
            result['source_audit_slot_sha256'] = sampled.get('native_wav_sha256') if sampled else None
            result['stable_audio_id'] = stable_id
    result.update(source_path=str(path), source_sha256=result['sha256'])
    return result


def load_accepted_cj_reference(project_root, target_identity, identity_confirmed):
    """Load the original CJ utterance used in the accepted experiment, for CJ only."""
    actor = re.sub(r'[^a-z]', '', str(target_identity or '').casefold())
    if actor not in ('cj', 'carl', 'carljohnson') or identity_confirmed is not True:
        raise ValueError('A referência CJ aceita só pode ser usada em fala CJ com identidade confirmada manualmente.')
    root = Path(project_root).resolve()
    report_path = root / _CJ_REPORT
    if not report_path.is_file():
        raise ValueError('Relatório da referência CJ original aceita não encontrado.')
    payload = json.loads(report_path.read_text(encoding='utf-8'))
    records = payload.get('records') if isinstance(payload, dict) else None
    row = next((item for item in (records or []) if item.get('id') == 'Track_078:04'), None)
    if not row:
        raise ValueError('A fala original CJ aceita Track_078:04 está ausente no relatório.')
    path = Path(row.get('reference_path') or '').resolve()
    allowed = (root / _CJ_REFERENCE_DIR).resolve()
    try:
        path.relative_to(allowed)
    except ValueError:
        raise ValueError('Referência CJ fora do diretório de WAVs originais alinhados.')
    if row.get('reference_relation') != 'SAME_ORIGINAL_UTTERANCE' or not str(row.get('alignment_authority', '')).startswith('NATIVE_CUE_AND_BLIND_ASR'):
        raise ValueError('Proveniência de alinhamento da referência CJ não é aceita.')
    source = Path(row.get('reference_source') or '').resolve()
    if not source.is_file() or sha256(source) != row.get('reference_source_sha256'):
        raise ValueError('Hash da faixa original CUTSCENE da referência CJ mudou.')
    if not path.is_file() or sha256(path) != row.get('reference_sha256'):
        raise ValueError('Hash da referência CJ original alinhada mudou.')
    transcript = str(row.get('reference_text') or '').strip()
    if not transcript:
        raise ValueError('Transcrição original da referência CJ ausente.')
    receipt_path = root / _CJ_REPORT.parent / 'Track_078_04_aligned_receipt.json'
    if not receipt_path.is_file():
        raise ValueError('Recibo da geração CJ aceita não encontrado; seed não será presumido.')
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    if (receipt.get('id') != row.get('id') or receipt.get('reference_sha256') != row.get('reference_sha256') or
            receipt.get('reference_text') != row.get('reference_text') or
            not isinstance(receipt.get('seed'), int)):
        raise ValueError('Recibo da referência CJ não concorda com o WAV/transcrição aceitos.')
    return {
        'path': str(path), 'sha256': row['reference_sha256'], 'reference_text': transcript,
        'seed': receipt['seed'],
        'actor_id': 'CJ', 'reference_kind': 'accepted_original_cutscene_utterance',
        'provenance': {
            'experiment_input_path': str(report_path), 'experiment_input_sha256': sha256(report_path),
            'utterance_id': row['id'], 'reference_source_path': str(source),
            'reference_source_sha256': row['reference_source_sha256'],
            'reference_start_seconds': row['reference_start_seconds'],
            'reference_end_seconds': row['reference_end_seconds'],
            'alignment_authority': row['alignment_authority'],
            'reference_relation': row['reference_relation'],
            'production_approved': False,
        },
    }


def build_command(project_root, reference_path, reference_text, target_text, output_path,
                  provenance, speed=1.0, duration=None, python_path=None, script_path=None,
                  window_seconds=None, mission=None, jamcrc=None, request_path=None, seed=None):
    """Write an immutable one-take request and return the cached-runtime command."""
    root = Path(project_root).resolve()
    reference = Path(reference_path).resolve()
    output = Path(output_path).resolve()
    python = Path(python_path or _runtime_python(root)).resolve()
    script = Path(script_path or config.APP_DIR / 'tools' / 'omnivoice_tts.py').resolve()
    if not python.is_file() or not script.is_file():
        raise ValueError('Runtime OmniVoice local/cached indisponível; nada foi baixado ou instalado.')
    if not reference.is_file():
        raise ValueError('WAV de referência original não encontrado.')
    if not isinstance(reference_text, str) or not reference_text.strip():
        raise ValueError('Transcrição original explícita obrigatória para a referência OmniVoice.')
    if not isinstance(target_text, str) or not target_text.strip():
        raise ValueError('Texto PT-BR da fala está vazio.')
    target_spoken = spoken_text(target_text)
    if not target_spoken:
        raise ValueError('O texto GXT contém apenas tags e não produz fala.')
    if is_nonverbal_marker(reference_text):
        raise ValueError('A referência é um marcador não verbal; escolha uma fala original do mesmo personagem.')
    if is_nonverbal_marker(target_text):
        raise ValueError('O alvo GXT é não verbal (ex.: SCREAM); este engine gera falas, não efeitos vocais.')
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        raise ValueError('Speed OmniVoice deve ser numérico.')
    if not math.isfinite(speed) or not 0.5 <= speed <= 2.0:
        raise ValueError('Speed OmniVoice deve ficar entre 0,5 e 2,0.')
    if window_seconds is not None:
        try:
            window_seconds = float(window_seconds)
        except (TypeError, ValueError):
            raise ValueError('Janela de timing original inválida.')
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError('Janela de timing original inválida.')
    from studio.tts.timing import resolve_duration
    duration_mode = duration
    duration = resolve_duration(duration, window_seconds)
    if not isinstance(provenance, dict) or provenance.get('source_kind') not in {
            'original_game_saat_cache', 'accepted_original_cutscene_utterance',
            'verified_native_cutscene_window', 'verified_npc_catalog_original'}:
        raise ValueError('Proveniência precisa identificar uma fonte de voz original verificada.')
    npc = provenance.get('source_kind') == 'verified_npc_catalog_original'
    if npc:
        if mission is not None or jamcrc is not None:
            raise ValueError('Geração NPC não aceita missão/hash GXT.')
        npc_row = validate_npc_provenance(provenance, reference_text.strip(), provenance.get('physical_id'), root)
        if reference != Path(provenance['source_path']).resolve():
            raise ValueError('Referência NPC deve ser o WAV original exato do slot.')
        if window_seconds != float(npc_row['duration_seconds']):
            raise ValueError('Janela NPC difere da duração original no catálogo verificado.')
    english_gxt = Path(provenance.get('base_english_gxt_path') or '')
    if not npc and (not english_gxt.is_file() or not provenance.get('base_english_gxt_sha256') or
            sha256(english_gxt) != provenance.get('base_english_gxt_sha256') or
            not provenance.get('base_english_gxt_id') or not provenance.get('base_english_text')):
        raise ValueError('O editor aberto está desatualizado para OmniVoice; feche e reabra o GXT Editor para carregar a proveniência do GXT original.')
    source_value = provenance.get('source_path')
    source_digest = provenance.get('source_sha256')
    if not source_value or not source_digest:
        raise ValueError('Caminho e hash da fala original são obrigatórios na proveniência.')
    source_path = Path(source_value).resolve()
    if not source_path.is_file() or sha256(source_path) != source_digest:
        raise ValueError('Hash da fala original mudou desde a seleção da referência.')
    if seed is None or seed == '':
        seed = secrets.randbelow(2**31)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1:
        raise ValueError('Seed OmniVoice deve ser um inteiro entre 0 e 2147483647.')
    if output.exists():
        raise ValueError('A saída já existe; gere em uma pasta nova para preservar takes anteriores.')
    request = {
        'schema': 'omnivoice-single-take/1', 'project_root': str(root),
        'reference_path': str(reference), 'reference_sha256': sha256(reference),
        'reference_text': reference_text.strip(), 'target_text': target_text,
        'spoken_target_text': target_spoken,
        'language': 'pt', 'speed': speed, 'duration': duration, 'num_step': 32,
        'duration_mode': duration_mode, 'timing_policy': 'free_by_default_explicit_native_hint_no_post_stretch_or_cut',
        'normalize_text': False, 'preprocess_prompt': False, 'postprocess_output': False,
        'pad_duration': 0.0, 'fade_duration': 0.0,
        'seed': seed,
        'output_path': str(output), 'window_seconds': float(window_seconds) if window_seconds is not None else None,
        'mission': mission, 'jamcrc': jamcrc, 'provenance': dict(provenance or {}),
        'scope': 'NPC' if npc else 'GXT', 'physical_id': provenance.get('physical_id') if npc else None,
        'gpu_guard': {'enabled': bool(config.CFG.get('omnivoice_gpu_guard', True)),
                      'min_free_mib': 5120, 'max_utilization_percent': 10},
    }
    request_target = Path(request_path).resolve() if request_path else output.with_suffix('.omnivoice-request.json')
    if request_target.exists():
        raise ValueError('O pedido OmniVoice já existe; cada geração precisa de um pedido novo.')
    request_target.parent.mkdir(parents=True, exist_ok=True)
    with request_target.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(request, ensure_ascii=False, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return [str(python), '-u', str(script), '--request', str(request_target)]


def _runtime_python(project_root):
    configured = config.CFG.get('tts_engines', {}).get('omnivoice', {}).get('python') if isinstance(config.CFG.get('tts_engines'), dict) else None
    configured = configured or config.CFG.get('python_omnivoice')
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else Path(project_root) / path
    return Path(project_root) / 'deliverables' / 'omnivoice_gta_four_actor_preview_v1' / 'env' / 'Scripts' / 'python.exe'
