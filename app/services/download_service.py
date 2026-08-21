"""Download orchestration and durable task status updates."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import exists, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import session_scope
from app.downloader import (
    DownloadError,
    download_audio,
    download_file_storage_value,
)
from app.logger import get_logger
from app.models import (
    ChannelVideo,
    DownloadLog,
    Transcription,
    TranscriptionStatus,
    Video,
    VideoStatus,
)


logger = get_logger(__name__)


def enqueue_catalog_videos(
    session: Session, *, limit: int = 20, channel_id: int | None = None
) -> list[Video]:
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    statement = select(ChannelVideo).where(
        ~exists(
            select(Video.id).where(Video.youtube_url == ChannelVideo.youtube_url)
        )
    )
    if channel_id is not None:
        statement = statement.where(ChannelVideo.channel_id == channel_id)
    catalog_videos = list(
        session.scalars(
            statement.order_by(
                ChannelVideo.published_at.desc().nullslast(),
                ChannelVideo.id.desc(),
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    )
    tasks = [
        Video(
            channel_id=item.channel_id,
            youtube_url=item.youtube_url,
            title=item.title,
            published_at=item.published_at,
            status=VideoStatus.PENDING.value,
        )
        for item in catalog_videos
    ]
    session.add_all(tasks)
    session.flush()
    session.add_all(
        [
            DownloadLog(
                video_id=task.id,
                level="INFO",
                message="Task automatically created from channel catalog",
            )
            for task in tasks
        ]
    )
    return tasks


def enqueue_catalog_batch(
    limit: int | None = None, *, channel_id: int | None = None
) -> int:
    batch_size = limit or get_settings().catalog_enqueue_batch_size
    with session_scope() as session:
        tasks = enqueue_catalog_videos(
            session, limit=batch_size, channel_id=channel_id
        )
        count = len(tasks)
    if count:
        logger.info("Automatically enqueued %s catalog video(s)", count)
    return count


def requeue_retryable_failures() -> int:
    settings = get_settings()
    with session_scope() as session:
        result = session.execute(
            update(Video)
            .where(
                Video.status == VideoStatus.FAILED.value,
                Video.retry_count < settings.max_retries,
            )
            .values(status=VideoStatus.PENDING.value)
        )
        count = result.rowcount or 0
    if count:
        logger.info("Requeued %s retryable failed task(s)", count)
    return count


def claim_pending_video(channel_id: int | None = None) -> int | None:
    with session_scope() as session:
        statement = select(Video).where(Video.status == VideoStatus.PENDING.value)
        if channel_id is not None:
            statement = statement.where(Video.channel_id == channel_id)
        video = session.scalar(
            statement.order_by(Video.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if video is None:
            return None
        video.status = VideoStatus.DOWNLOADING.value
        video.error_message = None
        session.add(
            DownloadLog(video_id=video.id, level="INFO", message="Download started")
        )
        return video.id


def download_video_now(video_id: int) -> bool:
    """Synchronously download one video task selected by videos.id, ignoring queue order."""
    with session_scope() as session:
        video = session.get(Video, video_id)
        if video is None:
            raise ValueError(f"Video task {video_id} does not exist")
        video.status = VideoStatus.DOWNLOADING.value
        video.error_message = None
        session.add(
            DownloadLog(video_id=video.id, level="INFO", message="Download started")
        )
    return process_video(video_id)


def process_video(video_id: int) -> bool:
    with session_scope() as session:
        video = session.get(Video, video_id)
        if video is None:
            logger.error("Video task %s no longer exists", video_id)
            return False
        youtube_url = video.youtube_url
        channel_name = video.channel.name

    logger.info("Downloading video task id=%s url=%s", video_id, youtube_url)
    try:
        result = download_audio(youtube_url, channel_name=channel_name)
        with session_scope() as session:
            video = session.get(Video, video_id)
            if video is None:
                logger.error("Video task %s disappeared after download", video_id)
                return False
            video.status = VideoStatus.COMPLETED.value
            video.file_path = download_file_storage_value(result.file_path)
            if not video.title:
                video.title = result.file_path.stem
            video.downloaded_at = datetime.now(timezone.utc)
            video.error_message = None
            if video.transcription is None:
                settings = get_settings()
                session.add(
                    Transcription(
                        video_id=video.id,
                        status=TranscriptionStatus.PENDING.value,
                        provider="whisper_cpp",
                        model=settings.whisper_model,
                        language=settings.whisper_language,
                    )
                )
            session.add(
                DownloadLog(
                    video_id=video_id,
                    level="INFO",
                    message=f"Download completed: {result.file_path}",
                )
            )
        logger.info("Download completed for task id=%s: %s", video_id, result.file_path)
        return True
    except (DownloadError, OSError) as exc:
        error_message = str(exc)[:10000]
        logger.exception("Download failed for task id=%s", video_id)
        try:
            with session_scope() as session:
                video = session.get(Video, video_id)
                if video is not None:
                    video.status = VideoStatus.FAILED.value
                    video.retry_count += 1
                    video.error_message = error_message
                    session.add(
                        DownloadLog(video_id=video_id, level="ERROR", message=error_message)
                    )
        except SQLAlchemyError:
            logger.exception("Could not persist failure state for task id=%s", video_id)
        return False
    except Exception as exc:
        error_message = f"Unexpected error: {exc}"[:10000]
        logger.exception("Unexpected download error for task id=%s", video_id)
        try:
            with session_scope() as session:
                video = session.get(Video, video_id)
                if video is not None:
                    video.status = VideoStatus.FAILED.value
                    video.retry_count += 1
                    video.error_message = error_message
                    session.add(
                        DownloadLog(video_id=video_id, level="ERROR", message=error_message)
                    )
        except SQLAlchemyError:
            logger.exception("Could not persist unexpected failure for task id=%s", video_id)
        return False


def process_pending_videos(channel_id: int | None = None) -> dict[str, int]:
    requeued = requeue_retryable_failures()
    completed = 0
    failed = 0
    while True:
        video_id = claim_pending_video(channel_id=channel_id)
        if video_id is None:
            break
        if process_video(video_id):
            completed += 1
        else:
            failed += 1
    summary = {"completed": completed, "failed": failed, "requeued": requeued}
    logger.info("Download cycle finished: %s", summary)
    return summary


def process_catalog_download_cycle(channel_id: int | None = None) -> dict[str, int]:
    settings = get_settings()
    enqueued = (
        enqueue_catalog_batch(channel_id=channel_id)
        if settings.auto_enqueue_catalog
        else 0
    )
    summary = process_pending_videos(channel_id=channel_id)
    return {"enqueued": enqueued, **summary}
