"""Create reviewed WAV derivatives through an optional FFmpeg executable."""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .analyzer import analyze_wav


_PROCESS_TIMEOUT = 120.0


def _raise_if_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Audio effect rendering was cancelled")


def _tempo_filters(tempo: float) -> list[str]:
    """Split tempo into FFmpeg atempo stages supported by older FFmpeg builds."""
    result = []
    while tempo < 0.5:
        result.append("atempo=0.5")
        tempo /= 0.5
    while tempo > 2.0:
        result.append("atempo=2")
        tempo /= 2.0
    result.append(f"atempo={tempo:.10g}")
    return result


def render_effect(
    source,
    destination,
    speed=1.0,
    pitch_semitones=0.0,
    reverb=0.0,
    ffmpeg=None,
    cancel_event=None,
) -> dict:
    """Render a new PCM16 WAV with optional speed, pitch and echo ambience.

    ``speed`` is the playback-rate multiplier (0.5 is half speed and twice
    the duration). Pitch shifts use semitones and preserve duration relative
    to the selected speed. The ``reverb`` control (0..1) blends three delayed
    reflections with ``aecho``; this is room-like ambience, not convolution
    reverb. The source is never modified, and destination creation is atomic.
    """
    try:
        speed = float(speed)
        pitch_semitones = float(pitch_semitones)
        reverb = float(reverb)
    except (TypeError, ValueError) as exc:
        raise ValueError("speed, pitch_semitones and reverb must be finite numbers") from exc
    if not all(math.isfinite(value) for value in (speed, pitch_semitones, reverb)):
        raise ValueError("speed, pitch_semitones and reverb must be finite numbers")
    if not 0.5 <= speed <= 2.0:
        raise ValueError("speed must be between 0.5 and 2.0")
    if not -12.0 <= pitch_semitones <= 12.0:
        raise ValueError("pitch_semitones must be between -12 and 12")
    if not 0.0 <= reverb <= 1.0:
        raise ValueError("reverb must be between 0 and 1")

    source_path = Path(source).expanduser().resolve(strict=True)
    destination_path = Path(destination).expanduser().resolve(strict=False)
    if source_path == destination_path:
        raise ValueError("Source and destination must be different files")
    if destination_path.exists():
        raise FileExistsError(destination_path)
    if destination_path.suffix.lower() != ".wav":
        raise ValueError("Destination must have a .wav extension")
    source_analysis = analyze_wav(source_path)
    if not source_analysis["valid"]:
        raise ValueError(f"Source WAV is invalid: {source_analysis['error']}")

    executable = ffmpeg or shutil.which("ffmpeg")
    if not executable or (os.path.sep in str(executable) and not Path(executable).is_file()):
        raise RuntimeError("FFmpeg was not found; install FFmpeg or pass its executable path")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{destination_path.stem}.", suffix=".wav", dir=destination_path.parent
    )
    os.close(handle)
    temp_path = Path(temp_name)
    command = [str(executable), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source_path)]
    filters = []
    ratio = 2.0 ** (pitch_semitones / 12.0)
    if abs(pitch_semitones) > 1e-9:
        shifted_rate = max(1, round(source_analysis["sample_rate"] * ratio))
        filters.extend((f"asetrate={shifted_rate}", f"aresample={source_analysis['sample_rate']}"))
    filters.extend(_tempo_filters(speed / ratio))
    if reverb > 0:
        decay_a, decay_b, decay_c = (0.35 * reverb, 0.2 * reverb, 0.12 * reverb)
        filters.append(f"aecho=0.9:0.9:65|130|195:{decay_a:.6g}|{decay_b:.6g}|{decay_c:.6g}")
    filters.append("alimiter=limit=0.95")
    command.extend(("-af", ",".join(filters), "-c:a", "pcm_s16le", "-f", "wav", str(temp_path)))

    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        started = time.monotonic()
        with tempfile.TemporaryFile(mode="w+b") as stderr_file:
            _raise_if_cancelled(cancel_event)
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    shell=False,
                    creationflags=creationflags,
                )
            except OSError as exc:
                raise RuntimeError(f"Could not start FFmpeg: {exc}") from exc
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    process.kill()
                    process.wait()
                    raise RuntimeError("Audio effect rendering was cancelled")
                if time.monotonic() - started > _PROCESS_TIMEOUT:
                    process.kill()
                    process.wait()
                    raise TimeoutError("FFmpeg exceeded the 120-second audio effect timeout")
                time.sleep(0.05)
            if process.returncode:
                stderr_file.seek(0)
                message = stderr_file.read(4000).decode("utf-8", "replace").strip()
                raise RuntimeError(f"FFmpeg failed with exit code {process.returncode}: {message or 'no diagnostics'}")

        analysis = analyze_wav(temp_path)
        if not analysis["valid"]:
            raise RuntimeError(f"FFmpeg produced an invalid WAV: {analysis['error']}")
        # Hard-link creation is atomic and fails if another writer created the
        # destination after our initial existence check. Both paths share a dir.
        _raise_if_cancelled(cancel_event)
        try:
            os.link(temp_path, destination_path)
        except FileExistsError:
            raise FileExistsError(destination_path) from None
        temp_path.unlink()
        return analysis
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
