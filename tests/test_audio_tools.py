import math
import io
import os
import shutil
import struct
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from studio.audio.analyzer import analyze_wav
from studio.audio.effects import render_effect


def write_pcm(path, samples, *, channels=1, rate=8000, width=2):
    with wave.open(str(path), "wb") as out:
        out.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        if width == 1:
            raw = bytes(max(0, min(255, int(round((x + 1) * 127.5)))) for x in samples)
        elif width == 2:
            raw = b"".join(struct.pack("<h", max(-32768, min(32767, int(x)))) for x in samples)
        elif width == 3:
            raw = b"".join(int(x).to_bytes(3, "little", signed=True) for x in samples)
        elif width == 4:
            raw = b"".join(struct.pack("<i", int(x)) for x in samples)
        else:
            raise ValueError(width)
        out.writeframes(raw)
    return path


class AudioAnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_reports_duration_silence_clipping_and_signed_waveform_bins(self):
        path = self.root / "levels.wav"
        # Four frames: silence, full positive, full negative, half positive.
        write_pcm(path, [0, 32767, -32768, 16384], rate=4)
        result = analyze_wav(path)
        self.assertTrue(result["valid"], result["error"])
        self.assertEqual(result["duration"], 1.0)
        self.assertEqual(result["sample_rate"], 4)
        self.assertEqual(result["channels"], 1)
        self.assertAlmostEqual(result["peak"], 1.0, places=5)
        self.assertEqual(result["clipping_ratio"], 0.5)
        self.assertEqual(result["silence_ratio"], 0.25)
        self.assertEqual(len(result["waveform"]), 4)
        self.assertEqual(result["waveform"][0], [0.0, 0.0])
        self.assertLess(result["waveform"][2][0], -0.99)
        self.assertGreater(result["waveform"][3][1], 0.49)

    def test_warns_when_take_exceeds_duration_fit_without_marking_it_invalid(self):
        path = self.root / "long.wav"
        write_pcm(path, [1000] * 32, rate=8)
        result = analyze_wav(path, original_duration=1.0)
        self.assertTrue(result["valid"])
        self.assertTrue(any("duration" in warning.lower() for warning in result["warnings"]))

    def test_rejects_truncated_and_malformed_wavs(self):
        truncated = self.root / "truncated.wav"
        write_pcm(truncated, [1000] * 80)
        truncated.write_bytes(truncated.read_bytes()[:-2])
        malformed = self.root / "malformed.wav"
        malformed.write_bytes(b"RIFF broken")
        self.assertFalse(analyze_wav(truncated)["valid"])
        self.assertFalse(analyze_wav(malformed)["valid"])

    def test_rejects_non_finite_float_samples(self):
        path = self.root / "nonfinite.wav"
        payload = b"RIFF" + struct.pack("<I", 36 + 4) + b"WAVE"
        payload += b"fmt " + struct.pack("<IHHIIHH", 16, 3, 1, 8000, 32000, 4, 32)
        payload += b"data" + struct.pack("<I", 4) + struct.pack("<f", math.nan)
        path.write_bytes(payload)
        result = analyze_wav(path)
        self.assertFalse(result["valid"])
        self.assertIn("finite", result["error"].lower())

    def test_decodes_pcm8_24_32_and_float64_samples(self):
        cases = (("pcm8", 1, [-1, 1]), ("pcm24", 3, [-8388608, 8388607]),
                 ("pcm32", 4, [-2147483648, 2147483647]))
        for name, width, values in cases:
            with self.subTest(format=name):
                path = self.root / f"{name}.wav"
                write_pcm(path, values, rate=2, width=width)
                result = analyze_wav(path)
                self.assertTrue(result["valid"], result["error"])
                self.assertAlmostEqual(result["peak"], 1.0, places=5)
                self.assertEqual(result["clipping_ratio"], 1.0)

        path = self.root / "float64.wav"
        payload = b"RIFF" + struct.pack("<I", 36 + 16) + b"WAVE"
        payload += b"fmt " + struct.pack("<IHHIIHH", 16, 3, 1, 2, 16, 8, 64)
        payload += b"data" + struct.pack("<I", 16) + struct.pack("<dd", -0.5, 0.75)
        path.write_bytes(payload)
        result = analyze_wav(path)
        self.assertTrue(result["valid"], result["error"])
        self.assertEqual(result["peak"], 0.75)


class AudioEffectsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.wav"
        samples = [int(12000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(8000)]
        write_pcm(self.source, samples, rate=8000)

    def tearDown(self):
        self.temp.cleanup()

    def test_slower_derivative_preserves_source_and_returns_analyzed_wav(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg is optional and not installed")
        before = self.source.read_bytes()
        destination = self.root / "slow.wav"
        result = render_effect(self.source, destination, speed=0.5, ffmpeg=ffmpeg)
        self.assertTrue(result["valid"], result["error"])
        self.assertGreater(result["duration"], 1.8)
        self.assertLess(result["duration"], 2.2)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertTrue(destination.is_file())

    def test_never_overwrites_existing_destination_or_source(self):
        destination = self.root / "kept.wav"
        destination.write_bytes(b"keep me")
        with self.assertRaises(FileExistsError):
            render_effect(self.source, destination, speed=0.9, ffmpeg="ffmpeg")
        with self.assertRaises(ValueError):
            render_effect(self.source, self.source, speed=0.9, ffmpeg="ffmpeg")
        self.assertEqual(destination.read_bytes(), b"keep me")

    def test_rejects_non_finite_or_out_of_range_effect_values(self):
        for kwargs in ({"speed": 0.0}, {"speed": 2.1}, {"pitch_semitones": 13}, {"reverb": math.nan}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    render_effect(self.source, self.root / "invalid.wav", ffmpeg="ffmpeg", **kwargs)

    def test_missing_ffmpeg_has_actionable_error(self):
        with self.assertRaisesRegex(RuntimeError, "FFmpeg"):
            render_effect(self.source, self.root / "out.wav", ffmpeg="missing-ffmpeg-audio-tools")

    def test_pitch_and_reflections_create_a_valid_derivative(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg is optional and not installed")
        before = self.source.read_bytes()
        destination = self.root / "pitched_ambient.wav"
        result = render_effect(
            self.source, destination, pitch_semitones=3, reverb=0.4, ffmpeg=ffmpeg
        )
        self.assertTrue(result["valid"], result["error"])
        self.assertGreater(result["duration"], 0.9)
        self.assertLess(result["duration"], 1.8)
        self.assertNotEqual(destination.read_bytes(), before)
        self.assertEqual(self.source.read_bytes(), before)

    def test_preset_cancellation_does_not_launch_or_publish(self):
        destination = self.root / "preset_cancel.wav"
        cancelled = threading.Event()
        cancelled.set()
        with patch("studio.audio.effects.subprocess.Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                render_effect(self.source, destination, ffmpeg="ffmpeg", cancel_event=cancelled)
        popen.assert_not_called()
        self.assertFalse(destination.exists())

    def test_cancel_during_fast_ffmpeg_exit_does_not_publish(self):
        destination = self.root / "fast_exit_cancel.wav"
        cancelled = threading.Event()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as out:
            out.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            out.writeframes(struct.pack("<h", 1200) * 800)
        valid_output = buffer.getvalue()

        class FastProcess:
            returncode = 0

            def poll(self):
                return 0

        def launch_and_cancel(command, **_kwargs):
            Path(command[-1]).write_bytes(valid_output)
            cancelled.set()
            return FastProcess()

        with patch("studio.audio.effects.subprocess.Popen", side_effect=launch_and_cancel):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                render_effect(self.source, destination, ffmpeg="ffmpeg", cancel_event=cancelled)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
