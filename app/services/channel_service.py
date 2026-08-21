"""Channel catalog synchronization, querying, and download promotion."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.channel_crawler import (
    CHANNEL_TABS,
    CrawledVideo,
    crawl_channel,
    crawl_channel_tab,
    normalize_channel_url,
)
from app.config import get_settings
from app.database import session_scope
from app.logger import get_logger
from app.models import (
    Channel,
    ChannelCrawlState,
    ChannelVideo,
    DownloadLog,
    Video,
    VideoStatus,
)
from app.services.video_service import DuplicateVideoError


logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ChannelSyncResult:
    channel_id: int
    discovered: int
    inserted: int
    updated: int


@dataclass(frozen=True, slots=True)
class ChannelBatchResult:
    state_id: int
    channel_id: int
    source_tab: str
    start_index: int
    end_index: int
    discovered: int
    inserted: int
    updated: int
    next_index: int
    completed_cycle: bool


def ensure_channel(
    session: Session,
    *,
    channel_url: str,
    channel_name: str | None = None,
    category: str | None = None,
) -> Channel:
    normalized_url = normalize_channel_url(channel_url)
    channel = session.scalar(select(Channel).where(Channel.url == normalized_url))
    if channel is None:
        fallback_name = normalized_url.rstrip("/").rsplit("/", 1)[-1]
        channel = Channel(
            name=(channel_name or fallback_name).strip(),
            url=normalized_url,
            category=category,
        )
        session.add(channel)
        session.flush()
    else:
        if channel_name and channel_name.strip():
            channel.name = channel_name.strip()
        if category is not None:
            channel.category = category
    return channel


def get_channel_by_url(session: Session, channel_url: str) -> Channel | None:
    normalized_url = normalize_channel_url(channel_url)
    return session.scalar(select(Channel).where(Channel.url == normalized_url))


def _values(channel_id: int, item: CrawledVideo) -> dict[str, object]:
    return {
        "channel_id": channel_id,
        "youtube_id": item.youtube_id,
        "youtube_url": item.youtube_url,
        "title": item.title,
        "view_count": item.view_count,
        "thumbnail_url": item.thumbnail_url,
        "source_tab": item.source_tab,
        "published_at": item.published_at,
        "metadata_updated_at": datetime.now(timezone.utc),
    }


def upsert_channel_videos(
    session: Session, channel_id: int, items: Iterable[CrawledVideo]
) -> tuple[int, int]:
    unique_items = {item.youtube_id: item for item in items}
    if not unique_items:
        return 0, 0

    youtube_ids = list(unique_items)
    existing_ids = set(
        session.scalars(
            select(ChannelVideo.youtube_id).where(
                ChannelVideo.youtube_id.in_(youtube_ids)
            )
        )
    )
    rows = [_values(channel_id, item) for item in unique_items.values()]
    dialect_name = session.get_bind().dialect.name

    if dialect_name == "postgresql":
        base_statement = postgresql_insert(ChannelVideo).values(rows)
        excluded = base_statement.excluded
        statement = base_statement.on_conflict_do_update(
            index_elements=[ChannelVideo.youtube_id],
            set_={
                "channel_id": excluded.channel_id,
                "youtube_url": excluded.youtube_url,
                "title": excluded.title,
                "view_count": excluded.view_count,
                "thumbnail_url": excluded.thumbnail_url,
                "source_tab": excluded.source_tab,
                "published_at": excluded.published_at,
                "metadata_updated_at": excluded.metadata_updated_at,
            },
        )
        session.execute(statement)
    elif dialect_name == "sqlite":
        base_statement = sqlite_insert(ChannelVideo).values(rows)
        excluded = base_statement.excluded
        statement = base_statement.on_conflict_do_update(
            index_elements=[ChannelVideo.youtube_id],
            set_={
                "channel_id": excluded.channel_id,
                "youtube_url": excluded.youtube_url,
                "title": excluded.title,
                "view_count": excluded.view_count,
                "thumbnail_url": excluded.thumbnail_url,
                "source_tab": excluded.source_tab,
                "published_at": excluded.published_at,
                "metadata_updated_at": excluded.metadata_updated_at,
            },
        )
        session.execute(statement)
    else:
        for item in unique_items.values():
            video = session.scalar(
                select(ChannelVideo).where(ChannelVideo.youtube_id == item.youtube_id)
            )
            if video is None:
                session.add(ChannelVideo(**_values(channel_id, item)))
                continue
            for key, value in _values(channel_id, item).items():
                setattr(video, key, value)

    inserted = len(unique_items.keys() - existing_ids)
    updated = len(unique_items) - inserted
    return inserted, updated


def sync_channel_catalog(
    *,
    channel_url: str,
    channel_name: str | None = None,
    category: str | None = None,
    tabs: tuple[str, ...] | None = None,
    batch_size: int = 200,
    max_videos_per_tab: int | None = None,
    progress_callback: Callable[[CrawledVideo], None] | None = None,
) -> ChannelSyncResult:
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    selected_tabs = tabs or get_settings().channel_sync_tabs

    with session_scope() as session:
        channel = ensure_channel(
            session,
            channel_url=channel_url,
            channel_name=channel_name,
            category=category,
        )
        channel_id = channel.id
        normalized_url = channel.url

    discovered = 0
    inserted = 0
    updated = 0
    batch: list[CrawledVideo] = []

    def persist_batch() -> None:
        nonlocal inserted, updated
        if not batch:
            return
        with session_scope() as session:
            added, changed = upsert_channel_videos(session, channel_id, batch)
        inserted += added
        updated += changed
        logger.info(
            "Channel id=%s progress: discovered=%s inserted=%s updated=%s",
            channel_id,
            discovered,
            inserted,
            updated,
        )
        batch.clear()

    for item in crawl_channel(
        normalized_url,
        selected_tabs,
        max_videos_per_tab=max_videos_per_tab,
    ):
        discovered += 1
        if progress_callback is not None:
            progress_callback(item)
        batch.append(item)
        if len(batch) >= batch_size:
            persist_batch()
    persist_batch()

    result = ChannelSyncResult(
        channel_id=channel_id,
        discovered=discovered,
        inserted=inserted,
        updated=updated,
    )
    logger.info("Channel catalog synchronized: %s", asdict(result))
    return result


def configure_channel_schedule(
    session: Session,
    *,
    channel_url: str,
    tabs: tuple[str, ...] = ("videos",),
    channel_name: str | None = None,
    category: str | None = None,
    batch_size: int = 10,
    interval_minutes: int = 30,
    reset_cursor: bool = False,
) -> list[ChannelCrawlState]:
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be greater than zero")
    selected_tabs = tuple(dict.fromkeys(tabs))
    if not selected_tabs or any(tab not in CHANNEL_TABS for tab in selected_tabs):
        raise ValueError("tabs must contain videos, shorts, and/or streams")

    channel = ensure_channel(
        session,
        channel_url=channel_url,
        channel_name=channel_name,
        category=category,
    )
    now = datetime.now(timezone.utc)
    states: list[ChannelCrawlState] = []
    for tab in selected_tabs:
        state = session.scalar(
            select(ChannelCrawlState).where(
                ChannelCrawlState.channel_id == channel.id,
                ChannelCrawlState.source_tab == tab,
            )
        )
        if state is None:
            state = ChannelCrawlState(
                channel_id=channel.id,
                source_tab=tab,
                batch_size=batch_size,
                interval_minutes=interval_minutes,
                next_index=1,
                next_run_at=now,
                enabled=True,
            )
            session.add(state)
        else:
            state.batch_size = batch_size
            state.interval_minutes = interval_minutes
            state.enabled = True
            state.next_run_at = now
            state.last_error = None
            if reset_cursor:
                state.next_index = 1
                state.completed_cycles = 0
        session.flush()
        states.append(state)
    return states


def list_channel_schedules(
    session: Session, *, channel_url: str | None = None
) -> list[ChannelCrawlState]:
    statement = select(ChannelCrawlState).join(Channel)
    if channel_url:
        statement = statement.where(Channel.url == normalize_channel_url(channel_url))
    statement = statement.order_by(Channel.name, ChannelCrawlState.source_tab)
    return list(session.scalars(statement))


def get_channel_schedule(
    session: Session, *, channel_url: str, source_tab: str
) -> ChannelCrawlState:
    state = session.scalar(
        select(ChannelCrawlState)
        .join(Channel)
        .where(
            Channel.url == normalize_channel_url(channel_url),
            ChannelCrawlState.source_tab == source_tab,
        )
    )
    if state is None:
        raise ValueError(
            f"Channel schedule not found for tab={source_tab}; run schedule-channel first"
        )
    return state


def set_channel_schedules_enabled(
    session: Session,
    *,
    channel_url: str,
    tabs: tuple[str, ...] | None,
    enabled: bool,
) -> int:
    normalized_url = normalize_channel_url(channel_url)
    statement = (
        select(ChannelCrawlState)
        .join(Channel)
        .where(Channel.url == normalized_url)
    )
    if tabs:
        statement = statement.where(ChannelCrawlState.source_tab.in_(tabs))
    states = list(session.scalars(statement))
    for state in states:
        state.enabled = enabled
        if enabled:
            state.next_run_at = datetime.now(timezone.utc)
    return len(states)


def _mark_batch_failed(state_id: int, error: Exception) -> None:
    try:
        with session_scope() as session:
            state = session.get(ChannelCrawlState, state_id)
            if state is not None:
                now = datetime.now(timezone.utc)
                state.last_run_at = now
                state.last_error = str(error)[:10000]
                state.next_run_at = now + timedelta(minutes=state.interval_minutes)
    except Exception:
        logger.exception("Could not persist channel batch failure state id=%s", state_id)


def process_channel_batch(
    state_id: int,
    *,
    progress_callback: Callable[[CrawledVideo], None] | None = None,
) -> ChannelBatchResult:
    with session_scope() as session:
        state = session.get(ChannelCrawlState, state_id)
        if state is None:
            raise ValueError(f"Channel crawl state not found: {state_id}")
        channel = session.get(Channel, state.channel_id)
        if channel is None:
            raise ValueError(f"Channel not found for crawl state: {state_id}")
        channel_id = channel.id
        channel_url = channel.url
        source_tab = state.source_tab
        start_index = state.next_index
        batch_size = state.batch_size
        interval_minutes = state.interval_minutes

    end_index = start_index + batch_size - 1
    try:
        items: list[CrawledVideo] = []
        for item in crawl_channel_tab(
            channel_url,
            source_tab,
            max_videos=batch_size,
            start_index=start_index,
        ):
            items.append(item)
            if progress_callback is not None:
                progress_callback(item)

        known_totals = [item.playlist_count for item in items if item.playlist_count]
        known_total = max(known_totals) if known_totals else None
        completed_cycle = not items or (
            known_total is not None and end_index >= known_total
        )
        next_index = 1 if completed_cycle else end_index + 1
        now = datetime.now(timezone.utc)

        with session_scope() as session:
            state = session.get(ChannelCrawlState, state_id)
            if state is None:
                raise ValueError(f"Channel crawl state not found: {state_id}")
            inserted, updated = upsert_channel_videos(session, channel_id, items)
            state.next_index = next_index
            state.last_run_at = now
            state.last_error = None
            state.next_run_at = now + timedelta(minutes=interval_minutes)
            if completed_cycle:
                state.completed_cycles += 1

        result = ChannelBatchResult(
            state_id=state_id,
            channel_id=channel_id,
            source_tab=source_tab,
            start_index=start_index,
            end_index=end_index,
            discovered=len(items),
            inserted=inserted,
            updated=updated,
            next_index=next_index,
            completed_cycle=completed_cycle,
        )
        logger.info("Channel batch completed: %s", asdict(result))
        return result
    except Exception as exc:
        _mark_batch_failed(state_id, exc)
        logger.exception(
            "Channel batch failed: state_id=%s range=%s-%s",
            state_id,
            start_index,
            end_index,
        )
        raise


def claim_due_channel_state() -> int | None:
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        state = session.scalar(
            select(ChannelCrawlState)
            .where(
                ChannelCrawlState.enabled.is_(True),
                or_(
                    ChannelCrawlState.next_run_at.is_(None),
                    ChannelCrawlState.next_run_at <= now,
                ),
            )
            .order_by(ChannelCrawlState.next_run_at.asc().nullsfirst())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if state is None:
            return None
        state.next_run_at = now + timedelta(minutes=state.interval_minutes)
        return state.id


def run_due_channel_batches() -> dict[str, int]:
    succeeded = 0
    failed = 0
    while True:
        state_id = claim_due_channel_state()
        if state_id is None:
            break
        try:
            process_channel_batch(state_id)
            succeeded += 1
        except Exception:
            failed += 1
    summary = {"succeeded": succeeded, "failed": failed}
    logger.info("Due channel batches finished: %s", summary)
    return summary


def list_channel_videos(
    session: Session,
    *,
    channel_url: str | None = None,
    limit: int = 100,
) -> list[ChannelVideo]:
    statement = select(ChannelVideo)
    if channel_url:
        normalized_url = normalize_channel_url(channel_url)
        statement = statement.join(Channel).where(Channel.url == normalized_url)
    statement = statement.order_by(
        ChannelVideo.published_at.desc().nullslast(), ChannelVideo.id.desc()
    ).limit(limit)
    return list(session.scalars(statement))


def enqueue_channel_video(session: Session, youtube_id: str) -> Video:
    catalog_video = session.scalar(
        select(ChannelVideo).where(ChannelVideo.youtube_id == youtube_id.strip())
    )
    if catalog_video is None:
        raise ValueError(f"Channel video not found: {youtube_id}")
    existing = session.scalar(
        select(Video).where(Video.youtube_url == catalog_video.youtube_url)
    )
    if existing is not None:
        raise DuplicateVideoError(f"Video task already exists with id={existing.id}")

    video = Video(
        channel_id=catalog_video.channel_id,
        youtube_url=catalog_video.youtube_url,
        title=catalog_video.title,
        published_at=catalog_video.published_at,
        status=VideoStatus.PENDING.value,
    )
    session.add(video)
    session.flush()
    session.add(
        DownloadLog(
            video_id=video.id,
            level="INFO",
            message=f"Task created from channel catalog video {youtube_id}",
        )
    )
    return video
