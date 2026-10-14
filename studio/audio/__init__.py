from .wav import (
    wav_duracao, classe_delta, resolver_wav, wav_info,
    converter_wav_pcm16_mono, _DUR_CACHE, _WAV_RES
)
from .player import Player
from .resolver import (
    BaseAudioResolver, resolver_path_projeto,
    detectar_gta_sa_instalacoes, detectar_saat_sfx
)

