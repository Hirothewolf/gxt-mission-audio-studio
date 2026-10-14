"""Bounded-memory structural and level analysis for common uncompressed WAVs.

``waveform`` contains up to 256 ``[minimum, maximum]`` pairs, normalized to
the signed full-scale range. Each bin spans an equal portion of the file and
includes all channels. These measurements identify obvious duration, silence,
and clipping issues; they do not assess speech quality or intelligibility.
"""

from __future__ import annotations

import math
import os
import struct
from pathlib import Path


_BINS = 256
_READ_BYTES = 1 << 20
_SILENCE_LEVEL = 1e-4
_CLIP_LEVEL = 0.999


def _invalid(error: str) -> dict:
    return {
        "valid": False,
        "error": error,
        "duration": None,
        "sample_rate": None,
        "channels": None,
        "peak": None,
        "clipping_ratio": None,
        "silence_ratio": None,
        "warnings": [],
        "waveform": [],
    }


def _read_format(file_obj, file_size: int):
    header = file_obj.read(12)
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
        raise ValueError("Not a supported RIFF/WAVE file")
    riff_end = struct.unpack_from("<I", header, 4)[0] + 8
    if riff_end > file_size or riff_end < 12:
        raise ValueError("Truncated RIFF/WAVE container")

    fmt = None
    data_offset = data_size = None
    position = 12
    while position + 8 <= riff_end:
        file_obj.seek(position)
        chunk = file_obj.read(8)
        if len(chunk) != 8:
            raise ValueError("Truncated WAV chunk header")
        chunk_id, chunk_size = chunk[:4], struct.unpack_from("<I", chunk, 4)[0]
        body = position + 8
        end = body + chunk_size
        padded_end = end + (chunk_size & 1)
        if padded_end > riff_end:
            raise ValueError("Truncated WAV chunk")
        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise ValueError("WAV format chunk is too short")
            file_obj.seek(body)
            raw = file_obj.read(min(chunk_size, 64))
            code, channels, rate, _byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", raw)
            if code == 0xFFFE:
                if len(raw) < 40:
                    raise ValueError("Truncated extensible WAV format")
                code = struct.unpack_from("<H", raw, 24)[0]
            fmt = (code, channels, rate, block_align, bits)
        elif chunk_id == b"data" and data_offset is None:
            data_offset, data_size = body, chunk_size
        position = padded_end
    if position != riff_end:
        raise ValueError("Truncated WAV chunk header")

    if fmt is None or data_offset is None:
        raise ValueError("WAV must contain format and audio data chunks")
    code, channels, rate, block_align, bits = fmt
    if channels < 1 or channels > 64 or rate < 1:
        raise ValueError("Invalid WAV channel count or sample rate")
    if code == 1 and bits in (8, 16, 24, 32):
        width = bits // 8
    elif code == 3 and bits in (32, 64):
        width = bits // 8
    else:
        raise ValueError(f"Unsupported WAV encoding (format={code}, bits={bits})")
    if block_align != channels * width:
        raise ValueError("Invalid WAV frame alignment")
    if data_size % block_align:
        raise ValueError("Audio data ends in a partial frame")
    if data_offset + data_size > file_size:
        raise ValueError("Audio data is truncated")
    return code, channels, rate, width, bits, data_offset, data_size


def _samples(raw: bytes, code: int, bits: int):
    if code == 1 and bits == 8:
        for value in raw:
            yield (value - 128) / 128.0
    elif code == 1 and bits == 16:
        for (value,) in struct.iter_unpack("<h", raw):
            yield value / 32768.0
    elif code == 1 and bits == 24:
        for offset in range(0, len(raw), 3):
            value = int.from_bytes(raw[offset:offset + 3], "little", signed=True)
            yield value / 8388608.0
    elif code == 1 and bits == 32:
        for (value,) in struct.iter_unpack("<i", raw):
            yield value / 2147483648.0
    elif bits == 32:
        for (value,) in struct.iter_unpack("<f", raw):
            if not math.isfinite(value):
                raise ValueError("WAV contains a non-finite sample")
            yield value
    else:
        for (value,) in struct.iter_unpack("<d", raw):
            if not math.isfinite(value):
                raise ValueError("WAV contains a non-finite sample")
            yield value


def analyze_wav(path, original_duration=None) -> dict:
    """Analyze an uncompressed PCM/IEEE-float WAV without loading it all in memory.

    Structural or sample errors are returned with ``valid=False``. Duration
    fit is advisory: a long but structurally valid file remains valid and gets
    a warning. ``waveform`` bins are normalized signed min/max pairs.
    """
    try:
        with open(Path(path), "rb") as file_obj:
            file_size = os.fstat(file_obj.fileno()).st_size
            code, channels, rate, width, bits, offset, data_size = _read_format(file_obj, file_size)
            block_align = channels * width
            frame_count = data_size // block_align
            if frame_count == 0:
                raise ValueError("WAV contains no audio frames")
            duration = frame_count / rate
            clip_level = 0.98 if code == 1 and bits == 8 else _CLIP_LEVEL
            bins = [[math.inf, -math.inf] for _ in range(min(_BINS, frame_count))]
            clipped = silent = sample_count = frame_index = 0
            peak = 0.0
            bytes_per_read = max(block_align, (_READ_BYTES // block_align) * block_align)
            file_obj.seek(offset)
            remaining = data_size
            while remaining:
                amount = min(remaining, bytes_per_read)
                raw = file_obj.read(amount)
                if len(raw) != amount:
                    raise ValueError("Audio data is truncated")
                frame_in_chunk = 0
                values = _samples(raw, code, bits)
                while frame_in_chunk < amount // block_align:
                    bin_index = min(len(bins) - 1, frame_index * len(bins) // frame_count)
                    current = bins[bin_index]
                    for _ in range(channels):
                        value = next(values)
                        magnitude = abs(value)
                        peak = max(peak, magnitude)
                        sample_count += 1
                        clipped += magnitude >= clip_level
                        silent += magnitude <= _SILENCE_LEVEL
                        current[0] = min(current[0], value)
                        current[1] = max(current[1], value)
                    frame_index += 1
                    frame_in_chunk += 1
                remaining -= amount

        warnings = []
        if duration > 30.0:
            warnings.append("Duration exceeds the 30-second review heuristic")
        if original_duration is not None:
            original = float(original_duration)
            if math.isfinite(original) and original > 0 and duration > max(1.5 * original, original + 1.5):
                warnings.append("Duration is much longer than the original reference")
        return {
            "valid": True,
            "error": None,
            "duration": duration,
            "sample_rate": rate,
            "channels": channels,
            "peak": peak,
            "clipping_ratio": clipped / sample_count,
            "silence_ratio": silent / sample_count,
            "warnings": warnings,
            "waveform": [[float(low), float(high)] for low, high in bins],
        }
    except (OSError, ValueError, struct.error, StopIteration) as exc:
        return _invalid(str(exc))
