"""Original BEATS dialogue references; never invent SCRIPT coordinates."""
import json
import re
from pathlib import Path
import numpy as np
import soundfile as sf
from studio.tts.omnivoice import sha256, spoken_text

_FOLDER = Path('reports/studio_rework_20261008/omnivoice_cast_utterance_references_v1')
def _words(text):
    return re.findall(r"\w+", spoken_text(text).casefold())

def resolve_police_reference(record, project_root):
    stable = str(record.get('stable_audio_id') or '')
    match = re.fullmatch(r'POLICE:(PRO@\d+B?)', stable)
    if not match:
        raise ValueError('A fala não pertence ao diálogo da viatura.')
    key = match.group(1).replace('@', '_')
    folder = Path(project_root).resolve() / _FOLDER
    receipt_path = folder / ('POLICE_' + key + '_canary_receipt.json')
    proof_path = folder / 'reference_asr' / ('POLICE_' + key + '.json')
    if not receipt_path.is_file() or not proof_path.is_file():
        raise ValueError('Esta fala está na faixa contínua BEATS/Track_010, e não em um slot SCRIPT. '
                         'Ainda falta um trecho original com transcrição conferida para gerar esta linha individualmente.')
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    proof = json.loads(proof_path.read_text(encoding='utf-8'))
    gid = record.get('gxt_stable_id')
    if receipt.get('id') != stable or not gid or receipt.get('gxt_id') != gid:
        raise ValueError('O trecho da viatura pertence a outra chave GXT.')
    if receipt.get('reference_relation') != 'SAME_ORIGINAL_UTTERANCE':
        raise ValueError('A referência da viatura não corresponde à fala original selecionada.')
    source = Path(receipt['reference_source']).resolve()
    clip = Path(receipt['reference_path']).resolve()
    if (sha256(source) != receipt['reference_source_sha256'] or
            sha256(clip) != receipt['reference_sha256'] or
            Path(proof.get('audio_path','')).resolve() != clip or
            proof.get('audio_sha256') != receipt['reference_sha256'] or
            proof.get('blind_no_target_prompt') is not True or
            not proof.get('transcript')):
        raise ValueError('A fonte ou a transcrição conferida da viatura mudou. O áudio foi preservado.')
    if _words(proof['transcript']) != _words(receipt['native_transcript']):
        raise ValueError('A transcrição conferida não corresponde ao trecho da viatura selecionado.')
    with sf.SoundFile(source) as audio, sf.SoundFile(clip) as reference:
        rate = audio.samplerate
        start = round(float(receipt['reference_start_seconds']) * rate)
        end = round(float(receipt['reference_end_seconds']) * rate)
        if start < 0 or end <= start or end > len(audio) or reference.samplerate != rate or len(reference) != end-start:
            raise ValueError('Os limites do trecho original da viatura não correspondem à referência.')
        audio.seek(start)
        if not np.array_equal(audio.read(end-start,dtype='float32',always_2d=True),reference.read(dtype='float32',always_2d=True)):
            raise ValueError('A referência não contém os mesmos frames do trecho original da viatura.')
    return {'path':str(clip), 'sha256':receipt['reference_sha256'],
        'reference_text':proof['transcript'], 'duration_seconds':(end-start)/rate,
        'provenance':{'source_kind':'verified_native_cutscene_window',
            'source_path':str(source),'source_sha256':receipt['reference_source_sha256'],
            'gxt_stable_id':gid,'reference_start_frame':start,'reference_end_frame':end,
            'source_sample_rate':rate,'reference_audio_id':stable,
            'stream_container':'BEATS','stream_track':'Track_010',
            'reference_transcript_authority':'SAVED_BLIND_ASR_ORIGINAL_UTTERANCE',
            'reference_asr_verification':'SAVED_BLIND_ASR_MATCH',
            'reference_mapping_path':str(receipt_path),'reference_mapping_sha256':sha256(receipt_path),
            'reference_asr_path':str(proof_path),'reference_asr_sha256':sha256(proof_path)}}

def resolve_stream_reference(record, project_root, english_text=None):
    """Resolve aligned original frames for BEATS/CUTSCENE without a SCRIPT fallback."""
    import math
    import uuid
    from studio.audio.stream_catalog import lookup_window
    if str(record.get('stable_audio_id') or '') == 'POLICE:PRO@24':
        # Preserve the individually audited opening reference when available.
        reference = resolve_police_reference(record, project_root)
        reference['timing_status'] = 'SAVED_EXACT_UTTERANCE'
        return reference
    row = lookup_window(record, project_root)
    if english_text is not None and _words(row.get('canonical_text','')) != _words(english_text):
        raise ValueError('O texto inglês da chave mudou desde o alinhamento da faixa. Atualize os vínculos antes de gerar.')
    source = Path(row['source_path']).resolve()
    if sha256(source) != row['source_sha256']:
        raise ValueError('A faixa original mudou desde a transcrição. Nenhuma voz alternativa foi usada.')
    if sha256(row['input_path']) != row['input_sha256']:
        raise ValueError('O alinhamento salvo da faixa mudou. Atualize os vínculos antes de gerar.')
    start,end = float(row['start']),float(row['end'])
    if not all(math.isfinite(x) for x in (start,end)) or start < 0 or end <= start:
        raise ValueError('Intervalo original inválido para esta fala.')
    with sf.SoundFile(source) as audio:
        rate=audio.samplerate;first,last=round(start*rate),round(end*rate)
        if first<0 or last<=first or last>len(audio):raise ValueError('O intervalo ultrapassa a faixa original.')
        audio.seek(first);samples=audio.read(last-first,dtype='float32',always_2d=True)
    identity = __import__('hashlib').sha256(json.dumps([row['source_sha256'],first,last]).encode()).hexdigest()[:24]
    folder=Path(project_root)/'.cache/stream_references'/row['container']/row['track'];folder.mkdir(parents=True,exist_ok=True)
    clip=folder/(identity+'.wav')
    if not clip.is_file():
        temporary=folder/(identity+'.'+uuid.uuid4().hex+'.tmp.wav')
        # Float preserves exact decoded OGG sample values for worker equality checks.
        sf.write(temporary,samples,rate,subtype='FLOAT');temporary.replace(clip)
    with sf.SoundFile(clip) as reference:
        if reference.samplerate!=rate or not np.array_equal(reference.read(dtype='float32',always_2d=True),samples):
            raise ValueError('O trecho em cache mudou; a referência original foi preservada.')
    audition = folder / (identity + '_ouvir.wav')
    if not audition.is_file():
        temporary = folder / (identity + '.' + uuid.uuid4().hex + '.preview.wav')
        sf.write(temporary, samples, rate, subtype='PCM_16')
        temporary.replace(audition)
    return {'path':str(clip.resolve()), 'audition_path':str(audition.resolve()),
        'sha256':sha256(clip),'reference_text':row['reference_text'],
        'duration_seconds':(last-first)/rate,'timing_status':row['status'],
        'provenance':{'source_kind':'verified_native_cutscene_window',
            'source_path':str(source),'source_sha256':row['source_sha256'],
            'gxt_stable_id':row['gid'],'reference_start_frame':first,'reference_end_frame':last,
            'source_sample_rate':rate,'reference_audio_id':record.get('stable_audio_id'),
            'stream_container':row['container'],'stream_track':row['track'],
            'reference_transcript_authority':row['timing_authority'],
            'reference_asr_verification':'SAVED_ALIGNMENT_CANDIDATE_NOT_LISTENING_APPROVAL',
            'reference_alignment_path':row['input_path'],'reference_alignment_sha256':row['input_sha256'],
            'overlapping_audio_ids':row.get('overlapping_audio_ids',[]),
            'overlap_voice_identity_verified':row.get('overlap_voice_identity_verified',False),
            'reference_is_partial':row.get('partial_reference',False),
            'caption_window_is_alignment_proof':False,'production_approved':False}}
