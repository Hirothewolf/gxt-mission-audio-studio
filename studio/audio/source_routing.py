"""Describe physical GTA audio sources independently from GXT mission tables."""
import json
import re
from pathlib import Path
from studio.tts.omnivoice import coordinates_from_record

_STREAM = re.compile(r'Track_\d{3}')

def is_stream_record(record):
    record = record or {}
    return (str(record.get('stable_audio_id') or '').upper().startswith(('POLICE:', 'CUTSCENE:', 'BEATS:'))
            or any(str(record.get(key) or '').upper() in {'BEATS', 'CUTSCENE'}
                   for key in ('pak', 'archive', 'package', 'container'))
            or (record.get('audio_route') or {}).get('family') == 'streams')

def audio_route(record):
    record = record or {}
    stable = str(record.get('stable_audio_id') or '')
    stream = record.get('stream') or {}
    container = str(record.get('pak') or record.get('container') or '').upper()
    if not container and stable.startswith(('CUTSCENE:', 'BEATS:')):
        container = stable.split(':', 1)[0]
    if is_stream_record(record) and any(record.get(k) is not None for k in ('bank_num','sound_num','bank_index','sound_index')):
        raise ValueError('Vínculo conflitante: a faixa contínua também recebeu coordenadas SFX.')
    if stable.startswith('POLICE:'):
        container, track = 'BEATS', 'Track_010'
    elif container in {'BEATS','CUTSCENE'}:
        track = stream.get('track') or record.get('stream_track')
    elif record.get('audio_route',{}).get('family') == 'streams':
        route = record['audio_route'];container,track=route.get('container'),route.get('track')
    else:
        try:
            pak,bank,sound=coordinates_from_record(record)
        except ValueError:
            return None
        return {'family':'SFX','container':pak,'bank':bank,'sound':sound,
                'destination':f'audio/SFX/{pak}/Bank_{bank:03d}/sound_{sound:03d}.wav',
                'label':f'SFX/{pak} · Bank_{bank:03d}/sound_{sound:03d}'}
    if record.get('cutscene_ambiguous') or container not in {'BEATS','CUTSCENE'} or not _STREAM.fullmatch(str(track)):
        raise ValueError('A faixa de áudio está ausente ou ambígua. Selecione a cena e o intervalo no estúdio de cutscenes.')
    return {'family':'streams','container':container,'track':track,
            'destination':f'audio/streams/{container}/{track}.ogg',
            'label':f'{container}/{track} · trecho de faixa contínua'}

def output_directory(run,route,table):
    if not re.fullmatch(r'[A-Za-z0-9_]+',str(table)):
        raise ValueError('Tabela GXT inválida para a pasta de saída.')
    root=Path(run)/'output'
    if not route:return root/table
    if route['family']=='streams':return root/'streams'/route['container']/route['track']/table
    return root/'SFX'/route['container']/('Bank_%03d'%route['bank'])/table

def command_output_directory(command, fallback=None):
    """Use the immutable request, including routed paths, for single/batch import."""
    if '--request' not in command:
        if fallback is None:raise ValueError('Pedido de geração não encontrado.')
        return Path(fallback)
    request=Path(command[command.index('--request')+1])
    data=json.loads(request.read_text(encoding='utf-8'))
    return Path(data['output_path']).resolve().parent
