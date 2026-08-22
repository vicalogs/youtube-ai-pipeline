"""Durable transcription queue and worker operations."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import exists, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import session_scope
from app.downloader import (
    DownloadError,
    download_file_storage_value,
    resolve_download_file_path,
)
from app.logger import get_logger
from app.models import Transcription, TranscriptionStatus, Video, VideoStatus
from app.transcriber import ProgressCallback, TranscriptionError, transcribe_audio


logger = get_logger(__name__)


def _resolve_video_audio_path(video: Video) -> Path:
    if not video.file_path:
        raise ValueError(f"Video {video.id} has no downloaded audio file")
    audio_path = resolve_download_file_path(video.file_path)
    if audio_path.is_file():
        try:
            portable_path = download_file_storage_value(audio_path)
        except DownloadError:
            pass
        else:
            if video.file_path != portable_path:
                logger.info(
                    "Migrated video id=%s audio path from %s to %s",
                    video.id,
                    video.file_path,
                    portable_path,
                )
                video.file_path = portable_path
    return audio_path


def enqueue_completed_transcriptions(
    session: Session, *, limit: int = 100
) -> list[Transcription]:
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    videos = list(
        session.scalars(
            select(Video)
            .where(
                Video.status == VideoStatus.COMPLETED.value,
                Video.file_path.is_not(None),
                ~exists(
                    select(Transcription.id).where(
                        Transcription.video_id == Video.id
                    )
                ),
            )
            .order_by(Video.downloaded_at.asc().nullsfirst(), Video.id.asc())
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    )
    settings = get_settings()
    tasks = [
        Transcription(
            video_id=video.id,
            status=TranscriptionStatus.PENDING.value,
            provider="whisper_cpp",
            model=settings.whisper_model,
            language=settings.whisper_language,
        )
        for video in videos
    ]
    session.add_all(tasks)
    session.flush()
    return tasks


def backfill_transcription_queue(limit: int = 100) -> int:
    with session_scope() as session:
        count = len(enqueue_completed_transcriptions(session, limit=limit))
    if count:
        logger.info("Created %s transcription task(s) for completed videos", count)
    return count


def requeue_retryable_transcriptions() -> int:
    settings = get_settings()
    with session_scope() as session:
        result = session.execute(
            update(Transcription)
            .where(
                Transcription.status == TranscriptionStatus.FAILED.value,
                Transcription.retry_count < settings.transcription_max_retries,
            )
            .values(status=TranscriptionStatus.PENDING.value)
        )
        count = result.rowcount or 0
    if count:
        logger.info("Requeued %s failed transcription task(s)", count)
    return count


def retry_failed_transcription(session: Session, task_id: int) -> Transcription:
    """Explicitly reset one failed or operator-interrupted task."""
    task = session.get(Transcription, task_id)
    if task is None:
        raise ValueError(f"Transcription task does not exist: {task_id}")
    retryable_statuses = {
        TranscriptionStatus.FAILED.value,
        TranscriptionStatus.TRANSCRIBING.value,
    }
    if task.status not in retryable_statuses:
        raise ValueError(
            f"Transcription task {task_id} is {task.status}, not failed or interrupted"
        )
    task.status = TranscriptionStatus.PENDING.value
    task.retry_count = 0
    task.error_message = None
    task.started_at = None
    task.completed_at = None
    session.flush()
    return task


def prepare_video_transcription(session: Session, video_id: int) -> Transcription:
    """Create or reset a task so one downloaded video can be transcribed now."""
    video = session.scalar(
        select(Video).where(Video.id == video_id).with_for_update()
    )
    if video is None:
        raise ValueError(f"Video does not exist: {video_id}")
    if video.status != VideoStatus.COMPLETED.value:
        raise ValueError(
            f"Video {video_id} is {video.status}; only completed downloads can be transcribed"
        )
    audio_path = _resolve_video_audio_path(video)
    if not audio_path.is_file():
        raise ValueError(f"Downloaded audio file does not exist: {audio_path}")

    task = session.scalar(
        select(Transcription)
        .where(Transcription.video_id == video_id)
        .with_for_update()
    )
    if task is not None and task.status == TranscriptionStatus.TRANSCRIBING.value:
        raise ValueError(f"Video {video_id} is already being transcribed")
    settings = get_settings()
    if task is None:
        task = Transcription(
            video_id=video_id,
            provider="whisper_cpp",
            model=settings.whisper_model,
            language=settings.whisper_language,
        )
        session.add(task)
    task.status = TranscriptionStatus.TRANSCRIBING.value
    task.retry_count = 0
    task.error_message = None
    task.started_at = datetime.now(timezone.utc)
    task.completed_at = None
    session.flush()
    return task


def transcribe_video_now(
    video_id: int, *, progress_callback: ProgressCallback | None = None
) -> bool:
    """Synchronously transcribe one video selected by videos.id."""
    with session_scope() as session:
        task_id = prepare_video_transcription(session, video_id).id
    return process_transcription(task_id, progress_callback=progress_callback)


def recover_stale_transcriptions() -> int:
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=settings.transcription_stale_minutes
    )
    with session_scope() as session:
        result = session.execute(
            update(Transcription)
            .where(
                Transcription.status == TranscriptionStatus.TRANSCRIBING.value,
                or_(
                    Transcription.started_at.is_(None),
                    Transcription.started_at < cutoff,
                ),
            )
            .values(
                status=TranscriptionStatus.PENDING.value,
                error_message="Recovered stale transcription task",
            )
        )
        count = result.rowcount or 0
    if count:
        logger.warning("Recovered %s stale transcription task(s)", count)
    return count


def claim_pending_transcription() -> int | None:
    with session_scope() as session:
        task = session.scalar(
            select(Transcription)
            .where(Transcription.status == TranscriptionStatus.PENDING.value)
            .order_by(Transcription.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if task is None:
            return None
        task.status = TranscriptionStatus.TRANSCRIBING.value
        task.started_at = datetime.now(timezone.utc)
        task.error_message = None
        return task.id


def _persist_failure(task_id: int, error_message: str) -> None:
    try:
        with session_scope() as session:
            task = session.get(Transcription, task_id)
            if task is not None:
                task.status = TranscriptionStatus.FAILED.value
                task.retry_count += 1
                task.error_message = error_message[:10000]
    except SQLAlchemyError:
        logger.exception("Could not persist transcription failure id=%s", task_id)


def process_transcription(
    task_id: int, *, progress_callback: ProgressCallback | None = None
) -> bool:
    with session_scope() as session:
        task = session.get(Transcription, task_id)
        if task is None:
            logger.error("Transcription task %s no longer exists", task_id)
            return False
        video = task.video
        if not video.file_path:
            _persist_failure(task_id, "Video has no downloaded audio file")
            return False
        audio_path = _resolve_video_audio_path(video)
        video_id = video.id
        video_title = video.title or audio_path.stem
        channel_name = video.channel.name

    logger.info(
        "Transcribing task id=%s video_id=%s audio=%s",
        task_id,
        video_id,
        audio_path,
    )
    try:
        result = transcribe_audio(
            audio_path,
            video_id=video_id,
            channel_name=channel_name,
            video_title=video_title,
            progress_callback=progress_callback,
        )
        with session_scope() as session:
            task = session.get(Transcription, task_id)
            if task is None:
                logger.error("Transcription task %s disappeared", task_id)
                return False
            task.status = TranscriptionStatus.COMPLETED.value
            task.transcript_text = result.transcript_text
            task.transcript_path = str(result.transcript_path)
            task.completed_at = datetime.now(timezone.utc)
            task.error_message = None
        logger.info("Transcription completed id=%s", task_id)
        return True
    except (TranscriptionError, OSError, ValueError) as exc:
        logger.exception("Transcription failed id=%s", task_id)
        _persist_failure(task_id, str(exc))
        return False
    except Exception as exc:
        logger.exception("Unexpected transcription failure id=%s", task_id)
        _persist_failure(task_id, f"Unexpected error: {exc}")
        return False


def process_next_transcription() -> dict[str, int]:
    backfilled = backfill_transcription_queue()
    recovered = recover_stale_transcriptions()
    requeued = requeue_retryable_transcriptions()
    task_id = claim_pending_transcription()
    if task_id is None:
        return {
            "backfilled": backfilled,
            "recovered": recovered,
            "requeued": requeued,
            "completed": 0,
            "failed": 0,
            "idle": 1,
        }
    completed = process_transcription(task_id)
    return {
        "backfilled": backfilled,
        "recovered": recovered,
        "requeued": requeued,
        "completed": int(completed),
        "failed": int(not completed),
        "idle": 0,
    }


def run_transcription_worker() -> None:
    settings = get_settings()
    logger.info(
        "Transcription worker started; poll_interval=%s second(s)",
        settings.transcription_poll_seconds,
    )
    try:
        while True:
            summary = process_next_transcription()
            logger.info("Transcription worker cycle: %s", summary)
            if summary["idle"]:
                time.sleep(settings.transcription_poll_seconds)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Transcription worker stopped")


def list_transcriptions(
    session: Session, *, status: str | None = None, limit: int = 100
) -> list[Transcription]:
    statement = select(Transcription)
    if status:
        allowed = {item.value for item in TranscriptionStatus}
        if status not in allowed:
            raise ValueError(f"Unknown transcription status: {status}")
        statement = statement.where(Transcription.status == status)
    statement = statement.order_by(Transcription.created_at.desc()).limit(limit)
    return list(session.scalars(statement))
