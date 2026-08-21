"""Stream channel video metadata from yt-dlp without downloading media."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Iterator
from urllib.parse import urlparse, urlunparse

from app.config import get_settings
from app.logger import get_logger


logger = get_logger(__name__)
CHANNEL_TABS = ("videos", "shorts", "streams")


class ChannelCrawlError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CrawledVideo:
    youtube_id: str
    youtube_url: str
    title: str
    view_count: int | None
    thumbnail_url: str | None
    published_at: datetime | None
    source_tab: str
    playlist_index: int | None = None
    playlist_count: int | None = None
    progress_total: int | None = None


def normalize_channel_url(url: str) -> str:
    normalized = url.strip()
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
    }:
        raise ValueError("A valid YouTube channel URL is required")

    parts = [part for part in parsed.path.split("/") if part]
    if not parts or parts[0] == "watch":
        raise ValueError("A YouTube channel URL, not a video URL, is required")
    if parts[-1].lower() in CHANNEL_TABS:
        parts.pop()
    if not parts:
        raise ValueError("A valid YouTube channel URL is required")

    path = "/" + "/".join(parts)
    return urlunparse(("https", "www.youtube.com", path, "", "", ""))


def _parse_published_at(metadata: dict[str, object]) -> datetime | None:
    timestamp = metadata.get("timestamp")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc)
        except (OSError, OverflowError, ValueError):
            pass

    upload_date = metadata.get("upload_date")
    if isinstance(upload_date, str):
        try:
            return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _thumbnail_url(metadata: dict[str, object]) -> str | None:
    thumbnail = metadata.get("thumbnail")
    if isinstance(thumbnail, str) and thumbnail:
        return thumbnail
    thumbnails = metadata.get("thumbnails")
    if isinstance(thumbnails, list):
        for item in reversed(thumbnails):
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                return item["url"]
    return None


def _parse_video(line: str, source_tab: str) -> CrawledVideo | None:
    try:
        metadata = json.loads(line)
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid JSON emitted by yt-dlp")
        return None

    youtube_id = metadata.get("id")
    title = metadata.get("title")
    if not isinstance(youtube_id, str) or not youtube_id:
        return None
    if not isinstance(title, str) or not title:
        return None

    webpage_url = metadata.get("webpage_url")
    if not isinstance(webpage_url, str) or not webpage_url:
        webpage_url = f"https://www.youtube.com/watch?v={youtube_id}"
    view_count = metadata.get("view_count")
    if not isinstance(view_count, int) or isinstance(view_count, bool):
        view_count = None
    playlist_index = metadata.get("playlist_index")
    if not isinstance(playlist_index, int) or isinstance(playlist_index, bool):
        playlist_index = None
    playlist_count = metadata.get("playlist_count")
    if not isinstance(playlist_count, int) or isinstance(playlist_count, bool):
        playlist_count = None

    return CrawledVideo(
        youtube_id=youtube_id,
        youtube_url=webpage_url,
        title=title,
        view_count=view_count,
        thumbnail_url=_thumbnail_url(metadata),
        published_at=_parse_published_at(metadata),
        source_tab=source_tab,
        playlist_index=playlist_index,
        playlist_count=playlist_count,
    )


def _read_stderr_tail(stderr_file, limit: int = 4000) -> str:
    stderr_file.flush()
    stderr_file.seek(0, os.SEEK_END)
    size = stderr_file.tell()
    stderr_file.seek(max(0, size - limit))
    return stderr_file.read().strip()


def crawl_channel_tab(
    channel_url: str,
    tab: str,
    max_videos: int | None = None,
    start_index: int = 1,
) -> Iterator[CrawledVideo]:
    if tab not in CHANNEL_TABS:
        raise ValueError(f"Unsupported channel tab: {tab}")
    if max_videos is not None and max_videos <= 0:
        raise ValueError("max_videos must be greater than zero")
    if start_index <= 0:
        raise ValueError("start_index must be greater than zero")
    if start_index != 1 and max_videos is None:
        raise ValueError("max_videos is required when start_index is not 1")
    tab_url = f"{normalize_channel_url(channel_url)}/{tab}"
    command = [
        "yt-dlp",
        "--skip-download",
        "--dump-json",
        "--ignore-errors",
        "--no-warnings",
        "--no-progress",
        "--socket-timeout",
        "30",
        "--retries",
        "3",
        "--extractor-retries",
        "3",
    ]
    if get_settings().ytdlp_proxy:
        command.extend(["--proxy", get_settings().ytdlp_proxy])
    if max_videos is not None:
        end_index = start_index + max_videos - 1
        command.extend(["--playlist-items", f"{start_index}-{end_index}"])
    command.append(tab_url)

    logger.info("Crawling channel tab: %s", tab_url)
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise ChannelCrawlError("yt-dlp executable was not found") from exc
        except OSError as exc:
            raise ChannelCrawlError(f"Could not start yt-dlp: {exc}") from exc

        assert process.stdout is not None
        try:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                video = _parse_video(line, tab)
                if video is not None:
                    if max_videos is not None:
                        limited_total = (
                            min(video.playlist_count, max_videos)
                            if video.playlist_count is not None
                            else max_videos
                        )
                        video = replace(video, progress_total=limited_total)
                    yield video
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise
        finally:
            process.stdout.close()

        return_code = process.wait()
        if return_code != 0:
            details = _read_stderr_tail(stderr_file) or "unknown yt-dlp error"
            raise ChannelCrawlError(
                f"yt-dlp exited with code {return_code} for {tab_url}: {details}"
            )


def crawl_channel(
    channel_url: str,
    tabs: tuple[str, ...] = CHANNEL_TABS,
    max_videos_per_tab: int | None = None,
) -> Iterator[CrawledVideo]:
    seen_ids: set[str] = set()
    for tab in tuple(dict.fromkeys(tabs)):
        for video in crawl_channel_tab(channel_url, tab, max_videos_per_tab):
            if video.youtube_id in seen_ids:
                continue
            seen_ids.add(video.youtube_id)
            yield video
