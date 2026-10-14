"""Native synthesis duration hints; never stretch or cut waveform samples."""
import math
from decimal import Decimal, ROUND_FLOOR

def initial_timing_settings(settings):
    # v2's original hint was an implicit default that could compress long text.
    version = settings.get('omnivoice_timing_policy_version')
    if version not in (2, 3):
        return 1.0, 'livre'
    duration = settings.get('omnivoice_duration', 'livre')
    if version == 2 and str(duration).strip().casefold() in ('original', 'auto', 'automatica', 'automática'):
        duration = 'livre'
    return settings.get('omnivoice_speed', 1.0), duration


def resolve_duration(value,original_seconds=None):
    text='' if value is None else str(value).strip().casefold()
    if text in ('','livre','free'):return None
    if text in ('original','auto','automatica','automática'):
        try:seconds=float(original_seconds)
        except (ValueError,TypeError):raise ValueError('Duração do áudio original indisponível; reindexe a fala ou escolha duração livre.')
        if not math.isfinite(seconds) or seconds<=0:
            raise ValueError('Duração do áudio original inválida; escolha uma referência original válida.')
        # Floor one decimal place: 2.47 -> 2.4. Never turn very short clips into zero.
        rounded=float(Decimal(str(seconds)).quantize(Decimal('0.1'),rounding=ROUND_FLOOR))
        return rounded if rounded>0 else seconds
    try:seconds=float(text.replace(',','.'))
    except ValueError:raise ValueError('Duração: use original, livre ou um número positivo em segundos.')
    if not math.isfinite(seconds) or seconds<=0:raise ValueError('Duração precisa ser positiva e finita.')
    return seconds
