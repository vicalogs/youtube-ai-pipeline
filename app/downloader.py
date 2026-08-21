"""Safe yt-dlp subprocess wrapper for MP3 downloads."""

from __future__ import annotations

import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings


class DownloadError(RuntimeError):
    """Raised when yt-dlp cannot download or convert a video."""


@dataclass(frozen=True, slots=True)
class DownloadResult:
    file_path: Path
    output: str


def download_file_storage_value(file_path: Path) -> str:
    """Return a portable path relative to the configured download directory."""
    download_root = get_settings().download_dir.resolve()
    resolved_path = file_path.expanduser().resolve()
    try:
        return resolved_path.relative_to(download_root).as_posix()
    except ValueError as exc:
        raise DownloadError(
            "Downloaded file is outside the configured download directory"
        ) from exc


def resolve_download_file_path(stored_path: str | Path) -> Path:
    """Resolve portable paths and recover relocated legacy absolute paths."""
    path = Path(stored_path).expanduser()
    if path.is_absolute():
        resolved_path = path.resolve()
        if resolved_path.is_file():
            return resolved_path
    download_root = get_settings().download_dir.resolve()
    if not path.is_absolute():
        candidate = (download_root / path).resolve()
        if not candidate.is_relative_to(download_root):
            raise DownloadError(
                "Downloaded audio path is outside the configured download directory"
            )
        return candidate

    # Versions before portable paths stored PROJECT_ROOT-dependent absolute paths.
    # Downloads always use DOWNLOAD_DIR/<channel>/<file>, so preserve that suffix
    # when a project or its database has moved to another machine/directory.
    candidate = (download_root / path.parent.name / path.name).resolve()
    if candidate.is_relative_to(download_root) and candidate.is_file():
        return candidate
    return resolved_path


def safe_directory_name(
    name: str, *, fallback: str = "unnamed", max_length: int = 120
) -> str:
    """Return a filesystem-safe directory component without changing its meaning."""
    normalized = unicodedata.normalize("NFKC", name).strip()
    invalid_characters = '<>:"/\\|?*'
    cleaned = "".join(
        "_" if character in invalid_characters or ord(character) < 32 else character
        for character in normalized
    ).strip(" .")
    if cleaned in {"", ".", ".."}:
        cleaned = fallback
    return cleaned[:max_length].rstrip(" .") or fallback


def safe_channel_directory_name(channel_name: str) -> str:
    return safe_directory_name(channel_name, fallback="unknown-channel")


def download_audio(youtube_url: str, *, channel_name: str | None = None) -> DownloadResult:
    settings = get_settings()
    download_root = settings.download_dir.resolve()
    channel_directory = settings.download_dir / safe_channel_directory_name(
        channel_name or "unknown-channel"
    )
    try:
        channel_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloadError(f"Cannot create download directory: {exc}") from exc

    resolved_channel_directory = channel_directory.resolve()
    if not resolved_channel_directory.is_relative_to(download_root):
        raise DownloadError("Channel download directory is outside the configured root")

    output_template = str(resolved_channel_directory / "%(title)s.%(ext)s")
    command = [
        "yt-dlp",
        "-f",
        "bestaudio",
        "-x",
        "--audio-format",
        "mp3",
        "--no-playlist",
        "--print",
        "after_move:filepath",
        "-o",
        output_template,
    ]
    if settings.ytdlp_proxy:
        command.extend(["--proxy", settings.ytdlp_proxy])
    command.append(youtube_url)

    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=settings.download_timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise DownloadError("yt-dlp executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(
            f"yt-dlp timed out after {settings.download_timeout_seconds} seconds"
        ) from exc
    except OSError as exc:
        raise DownloadError(f"Could not start yt-dlp: {exc}") from exc

    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "unknown yt-dlp error").strip()
        raise DownloadError(f"yt-dlp exited with code {completed.returncode}: {details}")

    printed_paths = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not printed_paths:
        raise DownloadError("yt-dlp succeeded but did not report the output file")

    file_path = Path(printed_paths[-1]).expanduser().resolve()
    if not file_path.is_relative_to(download_root):
        raise DownloadError("yt-dlp reported a file outside the configured download directory")
    if not file_path.is_file():
        raise DownloadError(f"Downloaded file does not exist: {file_path}")

    return DownloadResult(file_path=file_path, output=completed.stdout)
