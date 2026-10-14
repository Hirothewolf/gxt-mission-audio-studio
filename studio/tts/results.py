"""Validate legacy engine reports before registering pending takes."""
import json
from pathlib import Path
from studio.audio.analyzer import analyze_wav
from studio.core.utils import json_atomico


def import_generation_run(store, output, mission, texts, engine, original_durations=None):
    output = Path(output).resolve()
    original_durations = original_durations or {}
    result = {'valid': 0, 'quarantined': 0, 'missing': 0, 'details': [], 'imported': []}
    seen = set()
    report = output / 'redub_relatorio.json'
    try:
        data = json.loads(report.read_text(encoding='utf-8'))
        rows = data if isinstance(data, list) else data.get('linhas', [])
        if not isinstance(rows, list):
            raise ValueError('Relatório não contém lista de falas.')
    except (OSError, ValueError, AttributeError) as exc:
        result['missing'] = len(texts)
        result['details'].append({'error': 'Relatório ausente ou inválido: %s' % exc})
        return result
    for row in rows:
        try:
            h = int(str(row['jamcrc']).strip(), 16)
            if h not in texts or row.get('missao', mission) != mission or h in seen:
                raise ValueError('Identidade de fala ausente, duplicada ou fora do snapshot.')
            name = row.get('arquivo_audio') or row.get('wav_gerado')
            if not name:
                raise ValueError('Relatório sem nome de WAV.')
            path = (output / name).resolve()
            path.relative_to(output)
            if not path.is_file():
                result['details'].append({'jamcrc': '%08X' % h, 'error': 'WAV não encontrado'})
                continue
            seen.add(h)
            analysis = analyze_wav(path, original_durations.get(h))
            if not analysis.get('valid'):
                raise ValueError('WAV inválido: %s' % analysis.get('error'))
            duration = analysis['duration']
            original = original_durations.get(h)
            max_duration = min(30.0, max(original * 1.5, original + 1.5)) if original and original > 0 else 30.0
            # Small language-dependent timing differences go to human audition.
            # Keep an absolute runaway guard and the signal-integrity checks.
            review_limit = min(30.0, max(max_duration, original + 2.0)) if original and original > 0 else 30.0
            if duration > review_limit or analysis['silence_ratio'] > .98 or analysis['clipping_ratio'] > .01:
                raise ValueError('Suspeita técnica: duração/runaway, silêncio ou clipping; áudio preservado no run.')
            line_id = '%s|%08x' % (mission, h)
            if str(engine).casefold() == 'omnivoice':
                receipt_name = row.get('omnivoice_receipt')
                if not receipt_name or Path(receipt_name).is_absolute():
                    raise ValueError('Receipt OmniVoice por fala ausente ou inválido; take não será aceito.')
                receipt = (output / receipt_name).resolve()
                receipt.relative_to(output)
                candidate = store.import_omnivoice_candidate(line_id, path, texts[h], receipt)
            else:
                metadata = dict(row, engine=engine, run_output=str(output))
                metadata.update(duration_seconds=duration, window_seconds=original,
                                duration_override_required=duration > max_duration)
                candidate = store.import_candidate(line_id, path, texts[h], engine, metadata, analysis)
            result['imported'].append({'line_id': line_id, 'candidate_id': candidate['id']})
            result['valid'] += 1
        except (KeyError, TypeError, ValueError, OSError) as exc:
            result['quarantined'] += 1
            result['details'].append({'row': row, 'error': str(exc)})
    result['missing'] = len(set(texts) - seen)
    json_atomico(output / 'studio_validation.json', result)
    return result
