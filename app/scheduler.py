"""APScheduler setup for periodic downloads."""

from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.logger import get_logger
from app.services.channel_service import run_due_channel_batches
from app.services.download_service import process_catalog_download_cycle


logger = get_logger(__name__)


def run_download_cycle() -> None:
    try:
        process_catalog_download_cycle()
    except Exception:
        logger.exception("Scheduled download cycle failed")


def run_channel_sync_cycle() -> None:
    try:
        run_due_channel_batches()
    except Exception:
        logger.exception("Scheduled channel synchronization failed")


def create_scheduler() -> BlockingScheduler:
    settings = get_settings()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_download_cycle,
        trigger=IntervalTrigger(minutes=settings.scheduler_interval_minutes),
        id="download_pending_videos",
        name="Download pending YouTube videos",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        run_channel_sync_cycle,
        trigger=IntervalTrigger(
            minutes=settings.channel_batch_check_interval_minutes
        ),
        id="crawl_due_channel_batches",
        name="Crawl due YouTube channel batches",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    return scheduler


def start_scheduler() -> None:
    scheduler = create_scheduler()
    logger.info(
        "Scheduler started; interval=%s minute(s)",
        get_settings().scheduler_interval_minutes,
    )
    run_channel_sync_cycle()
    run_download_cycle()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
