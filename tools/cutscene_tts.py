"""One local Qwen take using the exact supplied audio and transcript."""
import argparse
import json
import os
from pathlib import Path
import hashlib
import time
import threading


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024*1024), b''):
            h.update(data)
    return h.hexdigest()


def event(message):
    print(json.dumps({'event': 'stage', 'message': message}, ensure_ascii=False), flush=True)


def generate(args):
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    model_dir = Path(args.model).resolve()
    cfg = json.loads((model_dir / 'config.json').read_text(encoding='utf-8'))
    if cfg.get('tts_model_type') != 'base':
        raise ValueError('A clonagem por referência exige um checkpoint Base.')
    reference = Path(args.reference).resolve()
    output = Path(args.output).resolve()
    if output.exists() or output == reference:
        raise ValueError('O take de saída deve ter um nome novo; arquivos existentes são preservados.')
    if not args.reference_text.strip() or not args.text.strip():
        raise ValueError('Texto e transcrição da referência são obrigatórios.')
    original_hash = sha(reference)
    if getattr(args, 'reference_sha256', None) and original_hash != args.reference_sha256:
        raise ValueError('O áudio de referência mudou antes da geração; o take não foi sintetizado.')
    non_streaming_mode = bool(getattr(args, 'non_streaming_mode', True))
    event('Carregando Qwen local; o primeiro carregamento pode demorar.')
    import torch
    import numpy as np
    import soundfile as sf
    from qwen_tts import Qwen3TTSModel
    info = sf.info(str(reference))
    if not 0.5 <= info.duration <= 30:
        raise ValueError('Use uma referência de 0,5 a 30 segundos, revisada por escuta.')
    device = ('cuda:0' if torch.cuda.is_available() else 'cpu') if args.device == 'auto' else args.device
    started = time.monotonic()
    model = Qwen3TTSModel.from_pretrained(str(model_dir), device_map=device,
              dtype=torch.float16 if device.startswith('cuda') else torch.float32,
              attn_implementation='sdpa', local_files_only=True)
    event('Criando referência da voz selecionada.')
    with torch.inference_mode():
        prompt = model.create_voice_clone_prompt(ref_audio=str(reference), ref_text=args.reference_text,
                                                x_vector_only_mode=False)
        event('Sintetizando somente o trecho solicitado.')
        waves, sr = model.generate_voice_clone(text=args.text, language='Portuguese',
                                               voice_clone_prompt=prompt, max_new_tokens=512,
                                               non_streaming_mode=non_streaming_mode)
    data = np.asarray(waves[0])
    if data.size == 0 or not np.isfinite(data).all() or sha(reference) != original_hash:
        raise ValueError('Take inválido ou referência alterada durante a geração.')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + '.part.wav')
    sf.write(str(temporary), data, sr, subtype='PCM_16')
    temporary.rename(output)
    report = {'model': str(model_dir), 'model_config_sha256': sha(model_dir / 'config.json'),
              'reference': str(reference), 'reference_sha256': original_hash,
              'reference_text': args.reference_text, 'text': args.text, 'device': device,
              'non_streaming_mode': non_streaming_mode,
              'duration': len(data)/sr, 'sample_rate': sr, 'output_sha256': sha(output),
              'elapsed_seconds': time.monotonic()-started, 'installed_in_game': False}
    output.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    event('Take salvo para revisão; nenhum áudio foi instalado no jogo.')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('model', 'reference', 'reference-text', 'text', 'output'):
        p.add_argument('--'+name, required=True)
    p.add_argument('--device', choices=['auto', 'cpu', 'cuda:0'], default='auto')
    p.add_argument('--reference-sha256', help='expected reference signature captured by the editor')
    p.add_argument('--non-streaming-mode', action='store_true', default=True,
                   help='condition voice generation on the complete utterance, as in the current production pipeline')
    args = p.parse_args()
    finished = threading.Event()
    began = time.monotonic()
    def heartbeat():
        while not finished.wait(10):
            event('Geração em andamento há %d s; aguarde ou cancele pela janela.' % (time.monotonic()-began))
    threading.Thread(target=heartbeat, name='tts-progress', daemon=True).start()
    try:
        generate(args)
    finally:
        finished.set()

if __name__ == '__main__':
    main()
