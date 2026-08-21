"""Stable extension contracts for future processing phases."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.transcriber import ProgressCallback, TranscriptionResult


class TranscriptionProvider(Protocol):
    def transcribe(
        self,
        audio_path: Path,
        *,
        video_id: int,
        channel_name: str,
        video_title: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> TranscriptionResult: ...


class SummaryProvider(Protocol):
    def summarize(self, transcript: str) -> str: ...


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> list[float]: ...
