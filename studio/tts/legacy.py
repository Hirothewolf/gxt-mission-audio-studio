# -*- coding: utf-8 -*-
"""Adapters for the locally verified Qwen and Chatterbox CLI scripts."""
from __future__ import annotations
from dataclasses import dataclass
import math
from pathlib import Path

from studio.tts.base import TTSCapabilities


@dataclass(frozen=True)
class LegacyEngine:
    id: str
    name: str
    python: Path
    script: Path
    root: Path

    def __post_init__(self):
        if self.id not in ('qwen', 'chatterbox'):
            raise ValueError(f'No verified CLI adapter for engine {self.id!r}.')

    @property
    def capabilities(self):
        return TTSCapabilities(
            voice_cloning=False,
            reference_audio=False,
            reference_text=False,
            emotion_control=self.id == 'chatterbox',
            speed_control=True,
            batch_generation=True,
            custom_checkpoint=False,
            offline_cpu=False,
        )

    def build_command(
        self,
        mission,
        gxt_path,
        output_dir,
        jamcrc=None,
        event=None,
        limit=0,
        max_speedup=None,
        exaggeration=None,
    ) -> list[str]:
        """Build only flags supported by the selected script's current CLI."""
        if self.id not in ('qwen', 'chatterbox'):
            raise ValueError(f'No verified CLI adapter for engine {self.id!r}.')
        def absolute_path(value):
            path = Path(value).expanduser()
            return path.resolve() if path.is_absolute() else (self.root / path).resolve()

        cmd = [
            str(self.python), str(self.script),
            '--mission', str(mission),
            '--gxt', str(absolute_path(gxt_path)),
            '--output-dir', str(absolute_path(output_dir)),
        ]
        if jamcrc:
            cmd.extend(['--jamcrc', str(jamcrc).replace('0x', '').replace('0X', '').upper()])
        if event:
            cmd.extend(['--event', str(event)])
        if limit:
            if int(limit) < 0:
                raise ValueError('limit must be zero or greater')
            cmd.extend(['--limit', str(int(limit))])
        if max_speedup is not None:
            value = float(max_speedup)
            if not math.isfinite(value) or value <= 0:
                raise ValueError('max_speedup must be greater than zero')
            cmd.extend(['--max-speedup', f'{value:.2f}'])
        if self.id == 'chatterbox' and exaggeration is not None:
            value = float(exaggeration)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError('exaggeration must be between 0 and 1')
            cmd.extend(['--exaggeration', f'{value:.2f}'])
        return cmd
