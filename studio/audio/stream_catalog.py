"""Index original stream windows by GXT identity; timings remain model candidates."""
import difflib
import json
import re
import uuid
from collections import defaultdict, Counter
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from studio.tts.omnivoice import sha256, spoken_text

_VARIANTS={'ten':'10','doughnut':'donut','shakin':'shaking','pendelbury':'pendlebury','pindleberry':'pendlebury'}
def tokens(text):
    return [_VARIANTS.get(t,t) for t in re.findall(r'[a-z0-9]+',spoken_text(text).lower())]

def align_dialogue(rows, words):
    expected=[];owners=[];spans=[]
    for index,row in enumerate(rows):
        first=len(expected);chunk=tokens(row['native_caption_en']);expected+=chunk
        owners += [index]*len(chunk);spans.append((first,len(expected)))
    actual=[];word_indices=[]
    for index,word in enumerate(words):
        chunk=tokens(word['word']);actual+=chunk;word_indices += [index]*len(chunk)
    matcher=difflib.SequenceMatcher(None,expected,actual,autojunk=False)
    matches=defaultdict(list);assigned=defaultdict(set)
    for tag,i,j,a,b in matcher.get_opcodes():
        if tag=='equal':
            for k in range(j-i):
                owner=owners[i+k];matches[owner].append(i+k);assigned[owner].add(word_indices[a+k])
        elif tag=='replace' and i<j and len(set(owners[i:j]))==1:
            assigned[owners[i]].update(word_indices[a:b])
        elif tag=='insert' and 0<i<len(owners) and owners[i-1]==owners[i]:
            assigned[owners[i]].update(word_indices[a:b])
    result=[]
    for index,(first,last) in enumerate(spans):
        ratio=len(matches[index])/max(1,last-first);ids=sorted(assigned[index])
        usable=ratio>=.5 and len(matches[index])>=2 and bool(ids)
        result.append({'usable':usable,'confidence':ratio,'partial':len(matches[index])<last-first,
                       'start':float(words[ids[0]]['start']) if ids else None,
                       'end':float(words[ids[-1]]['end']) if ids else None,
                       'transcript':' '.join(words[i]['word'].strip() for i in ids),
                       'word_indices':ids})
    return result

def refresh_available_catalog(project_root):
    """Rebuild saved stream evidence during audio reindex, if this project has it.

    A SCRIPT-only project is valid. Invalid existing stream evidence must still
    raise, so callers report a partial refresh and preserve the prior pointer.
    """
    root = Path(project_root)
    has_cutscenes = (root / 'reports/completed_native_windows_v2_141/manifest.jsonl').is_file()
    has_opening = (root / 'reports/studio_rework_20261007/gemini_voice_mapping_v1/input_manifest.json').is_file()
    has_asr = (root / 'reports/studio_rework_20261008/omnivoice_cast_utterance_references_v1/opening_native_asr/BEATS_010.json').is_file()
    if not has_cutscenes and not (has_opening and has_asr):
        return {}
    _, summary = build_catalog(root)
    return summary


def annotate_overlaps(records):
    """Expose simultaneous windows without clipping them or assigning voices."""
    groups = defaultdict(list)
    for row in records:
        row['overlapping_audio_ids'] = []
        row['overlap_voice_identity_verified'] = False
        if row.get('start') is not None and row.get('end') is not None:
            groups[(row['container'], row['track'], row.get('source_sha256'))].append(row)
    for rows in groups.values():
        for index, left in enumerate(rows):
            for right in rows[index + 1:]:
                if left['stable_audio_id'] == right['stable_audio_id']:
                    continue
                if max(float(left['start']), float(right['start'])) < min(float(left['end']), float(right['end'])):
                    left['overlapping_audio_ids'].append(right['stable_audio_id'])
                    right['overlapping_audio_ids'].append(left['stable_audio_id'])


def build_catalog(project_root):
    root=Path(project_root).resolve();records=[];inputs={}
    manifest=root/'reports/completed_native_windows_v2_141/manifest.jsonl'
    if manifest.is_file():
        inputs[str(manifest)]=sha256(manifest)
        for line in manifest.read_text(encoding='utf-8').splitlines():
            if not line.strip():continue
            row=json.loads(line);gid=row.get('subtitle_key')
            if not gid:continue
            alignment=row.get('word_alignment') or {};source=(row.get('source_evidence') or {}).get('native_audio') or {}
            labels={x.get('anonymous_label') for x in row.get('speech_windows',[]) if x.get('anonymous_label')}
            flags=alignment.get('flags',[]);reason=None
            if alignment.get('status')!='MATCH_CANDIDATE' or float(alignment.get('confidence') or 0)<.6:reason='Alinhamento de palavras insuficiente.'
            elif 'AMBIGUOUS_NONOVERLAPPING_MATCH' in flags or row.get('asr_word_ambiguity_blocks_timing'):reason='Mais de um intervalo possível para a mesma fala.'
            elif len(labels)>1:reason='O intervalo reúne mais de um grupo de voz; selecione o trecho no estúdio.'
            elif not source.get('path') or not source.get('sha256'):reason='Fonte original sem assinatura.'
            records.append({'gid':gid,'stable_audio_id':'CUTSCENE:%s:%s'%(row.get('scene'),row.get('cut_label')),
                'container':'CUTSCENE','track':row['track'],'source_path':source.get('path'),'source_sha256':source.get('sha256'),
                'start':alignment.get('audio_start'),'end':alignment.get('audio_end'),
                'reference_text':alignment.get('matched_native_text'),'canonical_text':row.get('canonical_native_en_raw'),
                'confidence':alignment.get('confidence'),'status':'UNAVAILABLE' if reason else 'ALIGNMENT_CANDIDATE',
                'reason':reason,'timing_authority':'SAVED_NATIVE_WORD_ALIGNMENT_CANDIDATE',
                'production_approved':False,'input_path':str(manifest),'input_sha256':inputs[str(manifest)],
                'speech_windows':row.get('speech_windows',[]),'alignment_flags':flags})
    opening=root/'reports/studio_rework_20261007/gemini_voice_mapping_v1/input_manifest.json'
    asr=root/'reports/studio_rework_20261008/omnivoice_cast_utterance_references_v1/opening_native_asr/BEATS_010.json'
    if opening.is_file() and asr.is_file():
        data=json.loads(opening.read_text(encoding='utf-8'));transcription=json.loads(asr.read_text(encoding='utf-8'))
        if transcription.get('blind_no_target_prompt') is not True:raise ValueError('ASR BEATS não confirma transcrição sem texto-alvo.')
        rows=[x for x in data['rows'] if x['id'].startswith('POLICE:')]
        native=data['audio']['BEATS_010']['native']
        if transcription['source_sha256']!=native['sha256']:raise ValueError('Fontes BEATS divergem entre a transcrição e o manifesto.')
        inputs.update({str(opening):sha256(opening),str(asr):sha256(asr)})
        for row,alignment in zip(rows,align_dialogue(rows,transcription['words'])):
            records.append({'gid':row['gxt_id'],'stable_audio_id':row['id'],'container':'BEATS','track':'Track_010',
                'source_path':transcription['source_path'],'source_sha256':transcription['source_sha256'],
                'start':alignment['start'],'end':alignment['end'],'reference_text':alignment['transcript'],
                'canonical_text':row['native_caption_en'],'confidence':alignment['confidence'],'partial_reference':alignment['partial'],
                'word_indices':alignment['word_indices'],'status':'ALIGNMENT_CANDIDATE' if alignment['usable'] else 'UNAVAILABLE',
                'reason':None if alignment['usable'] else 'Sem palavras suficientes para vincular esta legenda ao áudio.',
                'timing_authority':'SAVED_BLIND_ASR_WORD_ALIGNMENT_CANDIDATE','production_approved':False,
                'input_path':str(asr),'input_sha256':inputs[str(asr)],
                'export_source_path':native.get('source_ogg'),'export_source_sha256':native.get('source_ogg_sha256')})
    if not records:raise ValueError('Nenhum catálogo de intervalos originais foi encontrado neste projeto.')
    annotate_overlaps(records)
    folder=root/'workspace/stream_audio_catalog';folder.mkdir(parents=True,exist_ok=True)
    identity=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8]
    path=folder/('catalog_'+identity+'.json')
    data={'schema':'gta-stream-window-catalog/1','records':records,'inputs':inputs,
          'summary':dict(Counter((x['container']+':'+x['status']) for x in records))}
    with path.open('x',encoding='utf-8') as stream:json.dump(data,stream,ensure_ascii=False,indent=2)
    pointer=folder/'current.json';temporary=folder/('pointer_'+identity+'.tmp')
    temporary.write_text(json.dumps({'catalog':path.name,'sha256':sha256(path)}),encoding='utf-8');temporary.replace(pointer)
    return path,data['summary']

@lru_cache(maxsize=3)
def _load_catalog(pointer_path,size,mtime):
    pointer=Path(pointer_path);data=json.loads(pointer.read_text(encoding='utf-8'))
    path=(pointer.parent/data['catalog']).resolve()
    path.relative_to(pointer.parent.resolve())
    if sha256(path)!=data['sha256']:raise ValueError('O catálogo de faixas foi alterado; reconstrua o índice.')
    records=json.loads(path.read_text(encoding='utf-8'))['records'];groups=defaultdict(list)
    for record in records:groups[record['gid']].append(record)
    return dict(groups)

def _catalog_groups(project_root):
    pointer=Path(project_root)/'workspace/stream_audio_catalog/current.json'
    if not pointer.is_file():raise ValueError('O índice de faixas contínuas não foi preparado. Use "Atualizar vínculos de faixas".')
    st=pointer.stat();return _load_catalog(str(pointer),st.st_size,st.st_mtime_ns)

def _spoken(text):
    import re
    return ' '.join(re.sub(r'~[a-zA-Z0-9]~','',str(text or '')).split())

def catalog_rows(record,project_root):
    """Every saved candidate of a line, including UNAVAILABLE ones (never raises for missing data)."""
    try:groups=_catalog_groups(project_root)
    except (ValueError,OSError):return []
    return [dict(x) for x in groups.get((record or {}).get('gxt_stable_id'),[])]

def track_source(project_root,container,track):
    """Signed original file of a whole stream track, taken from the catalog."""
    signatures={(x.get('source_path'),x.get('source_sha256')) for rows in _catalog_groups(project_root).values()
                for x in rows if x['container']==container and x['track']==track and x.get('source_sha256')}
    if len(signatures)!=1:raise ValueError('A faixa %s/%s não tem uma única fonte original assinada no catálogo.'%(container,track))
    path,sha=signatures.pop();return dict(source_path=path,source_sha256=sha)

def list_tracks(project_root):
    """Tracks summary for pickers: lines, unavailable windows, manual windows and scenes."""
    from studio.audio import window_overrides
    manual=window_overrides.load(project_root);summary={}
    for gid,rows in _catalog_groups(project_root).items():
        for x in rows:
            item=summary.setdefault((x['container'],x['track']),dict(container=x['container'],track=x['track'],lines=0,unavailable=0,manual=0,scenes=set()))
            item['lines']+=1;item['unavailable']+=x['status']=='UNAVAILABLE'
            item['manual']+=bool(manual.get(gid,{}).get('track')==x['track'])
            scene=str(x.get('stable_audio_id') or '').split(':')
            if len(scene)>2:item['scenes'].add(scene[1])
    return [dict(v,scenes=sorted(v['scenes'])) for _,v in sorted(summary.items())]

def lookup_window(record,project_root):
    groups=_catalog_groups(project_root)
    from studio.audio.source_routing import audio_route
    from studio.audio import window_overrides
    route=audio_route(record)
    if not route or route['family']!='streams':raise ValueError('Esta fala não está vinculada a uma faixa contínua.')
    rows=[x for x in groups.get(record.get('gxt_stable_id'),[]) if x['container']==route['container'] and x['track']==route['track']
          and (not record.get('stable_audio_id') or x['stable_audio_id']==record['stable_audio_id'])]
    manual=window_overrides.get(project_root,record.get('gxt_stable_id'))
    if manual and manual.get('reference') and (manual['container'],manual['track'])==(route['container'],route['track']):
        # A listening-based manual choice resolves ambiguity and unavailable alignments.
        base=next((x for x in rows if x.get('source_sha256')==manual['source_sha256']),None)
        if base is None:raise ValueError('A janela manual desta fala foi gravada para outra versão da faixa original. Grave-a novamente.')
        ref=manual['reference']
        return dict(base,start=ref['start'],end=ref['end'],status='MANUAL_WINDOW',reason=None,
                    reference_text=ref.get('text') or base.get('reference_text') or _spoken(base.get('canonical_text')),
                    timing_authority=window_overrides.AUTHORITY,partial_reference=False,
                    manual_windows_path=str(window_overrides.path_for(project_root)),manual_updated_at=manual.get('updated_at'))
    signatures={(x.get('source_sha256'),x.get('start'),x.get('end')) for x in rows}
    if not rows or len(signatures)!=1:raise ValueError('A chave não tem um único intervalo de fala nesta faixa. Abra a cena da fala e grave a janela manual na timeline.')
    row=rows[0]
    if row['status']=='UNAVAILABLE':raise ValueError(row['reason']+' Abra a cena da fala e grave a janela manual na timeline.')
    if not row.get('reference_text'):raise ValueError('O trecho não tem transcrição original salva.')
    return dict(row)
