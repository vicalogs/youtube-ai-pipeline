"""Command-line entry point for the pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from tqdm import tqdm

from app.database import check_database_connection, init_database, session_scope
from app.logger import get_logger
from app.scheduler import start_scheduler
from app.services.channel_service import (
    configure_channel_schedule,
    enqueue_channel_video,
    get_channel_by_url,
    get_channel_schedule,
    list_channel_schedules,
    list_channel_videos,
    process_channel_batch,
    set_channel_schedules_enabled,
    sync_channel_catalog,
)
from app.services.download_service import (
    download_video_now,
    process_catalog_download_cycle,
)
from app.services.transcription_service import (
    list_transcriptions,
    process_next_transcription,
    retry_failed_transcription,
    run_transcription_worker,
    transcribe_video_now,
)
from app.services.video_service import add_video_task, list_videos
from app.transcriber import transcribe_audio


logger = get_logger(__name__)


class ChannelProgress:
    """Render one progress bar at a time for sequential channel tabs."""

    def __init__(self) -> None:
        self._bar: tqdm | None = None
        self._tab: str | None = None

    def update(self, item) -> None:
        if item.source_tab != self._tab:
            self.close()
            self._tab = item.source_tab
            self._bar = tqdm(
                total=item.progress_total or item.playlist_count,
                desc=f"采集 {item.source_tab}",
                unit="部",
                dynamic_ncols=True,
            )
        if self._bar is not None:
            total = item.progress_total or item.playlist_count
            if self._bar.total is None and total:
                self._bar.total = total
                self._bar.refresh()
            self._bar.update(1)

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="YouTube AI Audio Pipeline")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("init-db", help="Create missing database tables")
    subparsers.add_parser("check-db", help="Test the database connection")
    run_once_parser = subparsers.add_parser(
        "run-once", help="Process all currently pending tasks once"
    )
    run_once_parser.add_argument(
        "--channel-url",
        help="Only download pending tasks belonging to this channel",
    )
    subparsers.add_parser("scheduler", help="Run the periodic scheduler")
    subparsers.add_parser("transcribe-once", help="Process one transcription task")
    subparsers.add_parser(
        "transcription-worker", help="Continuously process transcription tasks"
    )

    download_video_parser = subparsers.add_parser(
        "download-video", help="Immediately download one video task by id"
    )
    download_video_parser.add_argument("--video-id", type=int, required=True)

    transcribe_video_parser = subparsers.add_parser(
        "transcribe-video", help="Immediately transcribe one downloaded video by videos.id"
    )
    transcribe_video_parser.add_argument("--video-id", type=int, required=True)
    transcribe_video_parser.add_argument(
        "--progress", action="store_true", help="Show FFmpeg and Whisper progress"
    )

    transcribe_audio_parser = subparsers.add_parser(
        "transcribe-audio", help="Transcribe an audio file directly without a database task"
    )
    transcribe_audio_parser.add_argument(
        "--audio-path", type=Path, required=True, help="Local audio file path"
    )
    transcribe_audio_parser.add_argument(
        "--channel-name",
        default="direct-audio",
        help="Output channel directory name (default: direct-audio)",
    )
    transcribe_audio_parser.add_argument(
        "--title", help="Output title directory name; defaults to the audio filename"
    )
    transcribe_audio_parser.add_argument(
        "--progress", action="store_true", help="Show FFmpeg and Whisper progress"
    )

    add_parser = subparsers.add_parser("add-video", help="Add a video task")
    add_parser.add_argument("--url", required=True, help="YouTube video URL")
    add_parser.add_argument("--channel-name", required=True)
    add_parser.add_argument("--channel-url", required=True)
    add_parser.add_argument("--category")
    add_parser.add_argument("--title")

    list_parser = subparsers.add_parser("list-videos", help="List video tasks")
    list_parser.add_argument("--status", choices=["pending", "downloading", "completed", "failed"])
    list_parser.add_argument("--limit", type=int, default=100)

    crawl_parser = subparsers.add_parser(
        "crawl-channel", help="Discover and update all videos from a channel"
    )
    crawl_parser.add_argument("--channel-url", required=True)
    crawl_parser.add_argument("--channel-name")
    crawl_parser.add_argument("--category")
    crawl_parser.add_argument(
        "--tabs",
        nargs="+",
        choices=["videos", "shorts", "streams"],
        help="Channel tabs to crawl; defaults to the CHANNEL_SYNC_TABS setting",
    )
    crawl_parser.add_argument(
        "--max-videos-per-tab",
        type=int,
        help="Limit each selected tab for local smoke testing",
    )

    catalog_parser = subparsers.add_parser(
        "list-channel-videos", help="List discovered channel videos"
    )
    catalog_parser.add_argument("--channel-url")
    catalog_parser.add_argument("--limit", type=int, default=100)

    enqueue_parser = subparsers.add_parser(
        "enqueue-channel-video", help="Create a download task from a discovered video"
    )
    enqueue_parser.add_argument("--youtube-id", required=True)

    schedule_parser = subparsers.add_parser(
        "schedule-channel", help="Configure persistent batch crawling for a channel"
    )
    schedule_parser.add_argument("--channel-url", required=True)
    schedule_parser.add_argument("--channel-name")
    schedule_parser.add_argument("--category")
    schedule_parser.add_argument(
        "--tabs",
        nargs="+",
        choices=["videos", "shorts", "streams"],
        default=["videos"],
    )
    schedule_parser.add_argument("--batch-size", type=int, default=10)
    schedule_parser.add_argument("--interval-minutes", type=int, default=30)
    schedule_parser.add_argument(
        "--reset", action="store_true", help="Reset the saved cursor to item 1"
    )

    schedules_parser = subparsers.add_parser(
        "list-channel-schedules", help="List channel batch crawl cursors"
    )
    schedules_parser.add_argument("--channel-url")

    batch_parser = subparsers.add_parser(
        "run-channel-batch", help="Immediately run the next saved channel batch"
    )
    batch_parser.add_argument("--channel-url", required=True)
    batch_parser.add_argument(
        "--tab", choices=["videos", "shorts", "streams"], default="videos"
    )

    toggle_parser = subparsers.add_parser(
        "set-channel-schedule", help="Pause or resume channel batch crawling"
    )
    toggle_parser.add_argument("--channel-url", required=True)
    toggle_parser.add_argument(
        "--tabs", nargs="+", choices=["videos", "shorts", "streams"]
    )
    toggle_parser.add_argument(
        "--enabled", choices=["true", "false"], required=True
    )

    transcription_parser = subparsers.add_parser(
        "list-transcriptions", help="List transcription tasks"
    )
    transcription_parser.add_argument(
        "--status", choices=["pending", "transcribing", "completed", "failed"]
    )
    transcription_parser.add_argument("--limit", type=int, default=100)

    retry_transcription_parser = subparsers.add_parser(
        "retry-transcription", help="Reset one failed or interrupted transcription task"
    )
    retry_transcription_parser.add_argument("--task-id", type=int, required=True)
    return parser


def run_command(args: argparse.Namespace) -> int:
    command = args.command or "scheduler"
    if command == "transcribe-audio":
        audio_path = args.audio_path.expanduser().resolve()
        if not audio_path.is_file():
            raise ValueError(f"Audio file does not exist: {audio_path}")
        if args.progress:
            with tqdm(
                total=100,
                desc="准备音频",
                unit="%",
                dynamic_ncols=True,
            ) as progress:
                current = 0

                def update_transcription_progress(percent: int, phase: str) -> None:
                    nonlocal current
                    percent = max(current, min(100, percent))
                    progress.set_description(phase)
                    progress.update(percent - current)
                    current = percent

                result = transcribe_audio(
                    audio_path,
                    video_id=0,
                    channel_name=args.channel_name,
                    video_title=args.title,
                    progress_callback=update_transcription_progress,
                )
        else:
            result = transcribe_audio(
                audio_path,
                video_id=0,
                channel_name=args.channel_name,
                video_title=args.title,
            )
        print(
            json.dumps(
                {
                    "audio_path": str(audio_path),
                    "transcript_path": str(result.transcript_path),
                },
                ensure_ascii=False,
            )
        )
    elif command == "check-db":
        check_database_connection()
        print("Database connection successful")
    elif command == "init-db":
        check_database_connection()
        init_database()
        print("Database initialized")
    elif command == "run-once":
        check_database_connection()
        init_database()
        channel_id = None
        if args.channel_url:
            with session_scope() as session:
                channel = get_channel_by_url(session, args.channel_url)
                if channel is None:
                    raise ValueError(f"Unknown channel URL: {args.channel_url}")
                channel_id = channel.id
        print(
            json.dumps(
                process_catalog_download_cycle(channel_id=channel_id),
                ensure_ascii=False,
            )
        )
    elif command == "download-video":
        check_database_connection()
        init_database()
        if args.video_id <= 0:
            raise ValueError("--video-id must be greater than zero")
        success = download_video_now(args.video_id)
        print(json.dumps({"video_id": args.video_id, "success": success}, ensure_ascii=False))
    elif command == "transcribe-once":
        check_database_connection()
        init_database()
        print(json.dumps(process_next_transcription(), ensure_ascii=False))
    elif command == "transcription-worker":
        check_database_connection()
        init_database()
        run_transcription_worker()
    elif command == "transcribe-video":
        check_database_connection()
        init_database()
        if args.video_id <= 0:
            raise ValueError("--video-id must be greater than zero")
        if args.progress:
            with tqdm(
                total=100,
                desc="准备音频",
                unit="%",
                dynamic_ncols=True,
            ) as progress:
                current = 0

                def update_transcription_progress(percent: int, phase: str) -> None:
                    nonlocal current
                    percent = max(current, min(100, percent))
                    progress.set_description(phase)
                    progress.update(percent - current)
                    current = percent

                completed = transcribe_video_now(
                    args.video_id,
                    progress_callback=update_transcription_progress,
                )
        else:
            completed = transcribe_video_now(args.video_id)
        print(
            json.dumps(
                {"video_id": args.video_id, "completed": completed},
                ensure_ascii=False,
            )
        )
        if not completed:
            return 1
    elif command == "add-video":
        check_database_connection()
        init_database()
        with session_scope() as session:
            video = add_video_task(
                session,
                youtube_url=args.url,
                channel_name=args.channel_name,
                channel_url=args.channel_url,
                category=args.category,
                title=args.title,
            )
            print(f"Created video task id={video.id}, status={video.status}")
    elif command == "list-videos":
        check_database_connection()
        init_database()
        if args.limit <= 0:
            raise ValueError("--limit must be greater than zero")
        with session_scope() as session:
            videos = list_videos(session, status=args.status, limit=args.limit)
            for video in videos:
                print(
                    json.dumps(
                        {
                            "id": video.id,
                            "youtube_url": video.youtube_url,
                            "title": video.title,
                            "status": video.status,
                            "file_path": video.file_path,
                            "retry_count": video.retry_count,
                            "error_message": video.error_message,
                        },
                        ensure_ascii=False,
                    )
                )
    elif command == "crawl-channel":
        check_database_connection()
        init_database()
        progress = ChannelProgress()
        try:
            result = sync_channel_catalog(
                channel_url=args.channel_url,
                channel_name=args.channel_name,
                category=args.category,
                tabs=tuple(args.tabs) if args.tabs else None,
                max_videos_per_tab=args.max_videos_per_tab,
                progress_callback=progress.update,
            )
        finally:
            progress.close()
        print(json.dumps(asdict(result), ensure_ascii=False))
    elif command == "list-channel-videos":
        check_database_connection()
        init_database()
        if args.limit <= 0:
            raise ValueError("--limit must be greater than zero")
        with session_scope() as session:
            videos = list_channel_videos(
                session, channel_url=args.channel_url, limit=args.limit
            )
            for video in videos:
                print(
                    json.dumps(
                        {
                            "id": video.id,
                            "channel_id": video.channel_id,
                            "youtube_id": video.youtube_id,
                            "title": video.title,
                            "view_count": video.view_count,
                            "youtube_url": video.youtube_url,
                            "thumbnail_url": video.thumbnail_url,
                            "source_tab": video.source_tab,
                            "published_at": (
                                video.published_at.isoformat()
                                if video.published_at
                                else None
                            ),
                        },
                        ensure_ascii=False,
                    )
                )
    elif command == "enqueue-channel-video":
        check_database_connection()
        init_database()
        with session_scope() as session:
            video = enqueue_channel_video(session, args.youtube_id)
            print(f"Created video task id={video.id}, status={video.status}")
    elif command == "schedule-channel":
        check_database_connection()
        init_database()
        with session_scope() as session:
            states = configure_channel_schedule(
                session,
                channel_url=args.channel_url,
                channel_name=args.channel_name,
                category=args.category,
                tabs=tuple(args.tabs),
                batch_size=args.batch_size,
                interval_minutes=args.interval_minutes,
                reset_cursor=args.reset,
            )
            for state in states:
                print(
                    json.dumps(
                        {
                            "state_id": state.id,
                            "channel_id": state.channel_id,
                            "source_tab": state.source_tab,
                            "batch_size": state.batch_size,
                            "next_index": state.next_index,
                            "interval_minutes": state.interval_minutes,
                            "enabled": state.enabled,
                            "next_run_at": state.next_run_at.isoformat(),
                        },
                        ensure_ascii=False,
                    )
                )
    elif command == "list-channel-schedules":
        check_database_connection()
        init_database()
        with session_scope() as session:
            states = list_channel_schedules(
                session, channel_url=args.channel_url
            )
            for state in states:
                print(
                    json.dumps(
                        {
                            "state_id": state.id,
                            "channel_name": state.channel.name,
                            "channel_url": state.channel.url,
                            "source_tab": state.source_tab,
                            "batch_size": state.batch_size,
                            "next_index": state.next_index,
                            "interval_minutes": state.interval_minutes,
                            "enabled": state.enabled,
                            "next_run_at": state.next_run_at.isoformat(),
                            "last_run_at": (
                                state.last_run_at.isoformat()
                                if state.last_run_at
                                else None
                            ),
                            "last_error": state.last_error,
                            "completed_cycles": state.completed_cycles,
                        },
                        ensure_ascii=False,
                    )
                )
    elif command == "run-channel-batch":
        check_database_connection()
        init_database()
        with session_scope() as session:
            state = get_channel_schedule(
                session, channel_url=args.channel_url, source_tab=args.tab
            )
            state_id = state.id
        progress = ChannelProgress()
        try:
            result = process_channel_batch(
                state_id, progress_callback=progress.update
            )
        finally:
            progress.close()
        print(json.dumps(asdict(result), ensure_ascii=False))
    elif command == "set-channel-schedule":
        check_database_connection()
        init_database()
        enabled = args.enabled == "true"
        with session_scope() as session:
            count = set_channel_schedules_enabled(
                session,
                channel_url=args.channel_url,
                tabs=tuple(args.tabs) if args.tabs else None,
                enabled=enabled,
            )
        print(json.dumps({"updated": count, "enabled": enabled}))
    elif command == "list-transcriptions":
        check_database_connection()
        init_database()
        if args.limit <= 0:
            raise ValueError("--limit must be greater than zero")
        with session_scope() as session:
            tasks = list_transcriptions(
                session, status=args.status, limit=args.limit
            )
            for task in tasks:
                print(
                    json.dumps(
                        {
                            "id": task.id,
                            "video_id": task.video_id,
                            "title": task.video.title,
                            "channel": task.video.channel.name,
                            "status": task.status,
                            "provider": task.provider,
                            "model": task.model,
                            "language": task.language,
                            "retry_count": task.retry_count,
                            "transcript_path": task.transcript_path,
                            "error_message": task.error_message,
                        },
                        ensure_ascii=False,
                    )
                )
    elif command == "retry-transcription":
        check_database_connection()
        init_database()
        if args.task_id <= 0:
            raise ValueError("--task-id must be greater than zero")
        with session_scope() as session:
            task = retry_failed_transcription(session, args.task_id)
            print(
                json.dumps(
                    {
                        "id": task.id,
                        "video_id": task.video_id,
                        "status": task.status,
                        "retry_count": task.retry_count,
                    },
                    ensure_ascii=False,
                )
            )
    elif command == "scheduler":
        check_database_connection()
        init_database()
        start_scheduler()
    else:
        raise ValueError(f"Unknown command: {command}")
    return 0


def main() -> int:
    logger.info("Application started")
    try:
        return run_command(build_parser().parse_args())
    except ValueError as exc:
        logger.error("Invalid request: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        logger.exception("Database operation failed")
        print(f"Database error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        logger.exception("Application failed")
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
