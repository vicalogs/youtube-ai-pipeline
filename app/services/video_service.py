"""Video and channel task management."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.channel_crawler import normalize_channel_url
from app.models import Channel, DownloadLog, Video, VideoStatus


class DuplicateVideoError(ValueError):
    pass


def validate_youtube_url(url: str) -> str:
    normalized = url.strip()
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    valid_host = host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
    has_video = bool(parsed.path.strip("/")) if host == "youtu.be" else bool(
        parse_qs(parsed.query).get("v") or parsed.path.startswith("/shorts/")
    )
    if parsed.scheme not in {"http", "https"} or not valid_host or not has_video:
        raise ValueError("A valid YouTube video URL is required")
    return normalized


def add_video_task(
    session: Session,
    *,
    youtube_url: str,
    channel_name: str,
    channel_url: str,
    category: str | None = None,
    title: str | None = None,
) -> Video:
    youtube_url = validate_youtube_url(youtube_url)
    channel_url = normalize_channel_url(channel_url)
    channel_name = channel_name.strip()
    if not channel_name or not channel_url:
        raise ValueError("channel_name and channel_url are required")

    existing_video = session.scalar(select(Video).where(Video.youtube_url == youtube_url))
    if existing_video:
        raise DuplicateVideoError(f"Video task already exists with id={existing_video.id}")

    channel = session.scalar(select(Channel).where(Channel.url == channel_url))
    if channel is None:
        channel = Channel(name=channel_name, url=channel_url, category=category)
        session.add(channel)
        session.flush()
    else:
        channel.name = channel_name
        if category is not None:
            channel.category = category

    video = Video(
        channel_id=channel.id,
        youtube_url=youtube_url,
        title=title,
        status=VideoStatus.PENDING.value,
    )
    session.add(video)
    try:
        session.flush()
    except IntegrityError as exc:
        raise DuplicateVideoError("Video task or channel already exists") from exc

    session.add(DownloadLog(video_id=video.id, level="INFO", message="Task created"))
    return video


def list_videos(
    session: Session, *, status: str | None = None, limit: int = 100
) -> list[Video]:
    statement = select(Video)
    if status:
        if status not in {item.value for item in VideoStatus}:
            raise ValueError(f"Unknown status: {status}")
        statement = statement.where(Video.status == status)
    statement = statement.order_by(Video.created_at.desc()).limit(limit)
    return list(session.scalars(statement))
