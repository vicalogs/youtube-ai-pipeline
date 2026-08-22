"""Application settings loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw_value!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _project_path(raw_value: str) -> Path:
    path = Path(raw_value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _boolean(name: str, default: bool) -> bool:
    raw_value = os.getenv(name, str(default)).strip().lower()
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    download_dir: Path
    log_file: Path
    scheduler_interval_minutes: int
    max_retries: int
    download_timeout_seconds: int
    log_level: str
    channel_sync_tabs: tuple[str, ...] = ("videos", "shorts", "streams")
    ytdlp_proxy: str | None = None
    channel_batch_check_interval_minutes: int = 1
    auto_enqueue_catalog: bool = True
    catalog_enqueue_batch_size: int = 20
    whisper_cpp_binary: Path = PROJECT_ROOT / "bin" / "whisper-cli"
    whisper_model_path: Path = PROJECT_ROOT / "models" / "whisper" / "ggml-small.bin"
    whisper_model: str = "small"
    whisper_language: str = "zh"
    whisper_threads: int = 3
    # whisper.cpp emits Chinese without punctuation unless the decoder is
    # primed with a punctuated sample sentence.
    whisper_prompt: str = "以下是普通话的句子。"
    transcript_dir: Path = PROJECT_ROOT / "transcripts"
    transcription_max_retries: int = 3
    transcription_poll_seconds: int = 10
    transcription_timeout_seconds: int = 7200
    transcription_stale_minutes: int = 120


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("DATABASE_URL is required; set it in .env")

    download_dir = _project_path(os.getenv("DOWNLOAD_DIR", "audio/downloads"))
    log_file = _project_path(os.getenv("LOG_FILE", "logs/app.log"))

    raw_tabs = os.getenv("CHANNEL_SYNC_TABS", "videos,shorts,streams")
    tabs = tuple(dict.fromkeys(tab.strip().lower() for tab in raw_tabs.split(",") if tab.strip()))
    allowed_tabs = {"videos", "shorts", "streams"}
    if not tabs or any(tab not in allowed_tabs for tab in tabs):
        raise ValueError(
            "CHANNEL_SYNC_TABS must contain videos, shorts, and/or streams"
        )

    return Settings(
        database_url=database_url,
        download_dir=download_dir,
        log_file=log_file,
        scheduler_interval_minutes=_positive_int("SCHEDULER_INTERVAL_MINUTES", 30),
        max_retries=_positive_int("MAX_RETRIES", 3),
        download_timeout_seconds=_positive_int("DOWNLOAD_TIMEOUT_SECONDS", 3600),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        channel_sync_tabs=tabs,
        ytdlp_proxy=os.getenv("YTDLP_PROXY", "").strip() or None,
        channel_batch_check_interval_minutes=_positive_int(
            "CHANNEL_BATCH_CHECK_INTERVAL_MINUTES", 1
        ),
        auto_enqueue_catalog=_boolean("AUTO_ENQUEUE_CATALOG", True),
        catalog_enqueue_batch_size=_positive_int("CATALOG_ENQUEUE_BATCH_SIZE", 20),
        whisper_cpp_binary=_project_path(
            os.getenv("WHISPER_CPP_BINARY", "bin/whisper-cli")
        ),
        whisper_model_path=_project_path(
            os.getenv("WHISPER_MODEL_PATH", "models/whisper/ggml-small.bin")
        ),
        whisper_model=os.getenv("WHISPER_MODEL", "small").strip() or "small",
        whisper_language=os.getenv("WHISPER_LANGUAGE", "zh").strip() or "zh",
        whisper_threads=_positive_int("WHISPER_THREADS", 3),
        whisper_prompt=os.getenv("WHISPER_PROMPT", "以下是普通话的句子。"),
        transcript_dir=_project_path(os.getenv("TRANSCRIPT_DIR", "transcripts")),
        transcription_max_retries=_positive_int("TRANSCRIPTION_MAX_RETRIES", 3),
        transcription_poll_seconds=_positive_int("TRANSCRIPTION_POLL_SECONDS", 10),
        transcription_timeout_seconds=_positive_int(
            "TRANSCRIPTION_TIMEOUT_SECONDS", 7200
        ),
        transcription_stale_minutes=_positive_int(
            "TRANSCRIPTION_STALE_MINUTES", 120
        ),
    )
