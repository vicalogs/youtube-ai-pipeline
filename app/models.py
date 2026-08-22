"""SQLAlchemy ORM models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class VideoStatus(StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"


class TranscriptionStatus(StrEnum):
    PENDING = "pending"
    TRANSCRIBING = "transcribing"
    COMPLETED = "completed"
    FAILED = "failed"


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    category: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    videos: Mapped[list["Video"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )
    catalog_videos: Mapped[list["ChannelVideo"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )
    crawl_states: Mapped[list["ChannelCrawlState"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )


class ChannelVideo(Base):
    """A discovered channel video; discovery does not enqueue a download."""

    __tablename__ = "channel_videos"
    __table_args__ = (
        UniqueConstraint("youtube_id", name="uq_channel_videos_youtube_id"),
        UniqueConstraint("youtube_url", name="uq_channel_videos_youtube_url"),
        Index("ix_channel_videos_channel_published", "channel_id", "published_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    youtube_id: Mapped[str] = mapped_column(String(32), nullable=False)
    youtube_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    view_count: Mapped[int | None] = mapped_column(BigInteger)
    thumbnail_url: Mapped[str | None] = mapped_column(String(4096))
    source_tab: Mapped[str] = mapped_column(String(20), nullable=False, default="videos")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    metadata_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    channel: Mapped[Channel] = relationship(back_populates="catalog_videos")


class ChannelCrawlState(Base):
    """Persistent cursor and schedule for one channel tab."""

    __tablename__ = "channel_crawl_states"
    __table_args__ = (
        UniqueConstraint(
            "channel_id", "source_tab", name="uq_channel_crawl_states_channel_tab"
        ),
        Index("ix_channel_crawl_states_due", "enabled", "next_run_at"),
        CheckConstraint("batch_size > 0", name="ck_channel_crawl_states_batch_size"),
        CheckConstraint(
            "interval_minutes > 0",
            name="ck_channel_crawl_states_interval_minutes",
        ),
        CheckConstraint("next_index > 0", name="ck_channel_crawl_states_next_index"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_tab: Mapped[str] = mapped_column(String(20), nullable=False, default="videos")
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    next_index: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    completed_cycles: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    channel: Mapped[Channel] = relationship(back_populates="crawl_states")


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'downloading', 'completed', 'failed')",
            name="ck_videos_status",
        ),
        CheckConstraint("retry_count >= 0", name="ck_videos_retry_count"),
        Index("ix_videos_status_created_at", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    youtube_url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    title: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=VideoStatus.PENDING.value,
        server_default=VideoStatus.PENDING.value,
    )
    file_path: Mapped[str | None] = mapped_column(String(4096))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    channel: Mapped[Channel] = relationship(back_populates="videos")
    download_logs: Mapped[list["DownloadLog"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )
    transcription: Mapped["Transcription | None"] = relationship(
        back_populates="video", cascade="all, delete-orphan", uselist=False
    )


class DownloadLog(Base):
    __tablename__ = "download_logs"
    __table_args__ = (Index("ix_download_logs_video_created", "video_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    video: Mapped[Video] = relationship(back_populates="download_logs")


class Transcription(Base):
    __tablename__ = "transcriptions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'transcribing', 'completed', 'failed')",
            name="ck_transcriptions_status",
        ),
        CheckConstraint("retry_count >= 0", name="ck_transcriptions_retry_count"),
        Index("ix_transcriptions_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=TranscriptionStatus.PENDING.value,
        server_default=TranscriptionStatus.PENDING.value,
    )
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False, default="whisper_cpp", server_default="whisper_cpp"
    )
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="small")
    language: Mapped[str] = mapped_column(String(20), nullable=False, default="zh")
    transcript_text: Mapped[str | None] = mapped_column(Text)
    transcript_path: Mapped[str | None] = mapped_column(String(4096))
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    video: Mapped[Video] = relationship(back_populates="transcription")
