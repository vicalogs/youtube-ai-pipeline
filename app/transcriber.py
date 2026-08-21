"""Local whisper.cpp transcription provider."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.captions import (
    captions_as_dicts,
    serialize_srt,
    transcript_text,
    whisper_json_to_captions,
)
from app.config import get_settings
from app.downloader import safe_channel_directory_name, safe_directory_name


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    transcript_text: str
    transcript_path: Path
    captions_path: Path
    srt_path: Path
    raw_json_path: Path


ProgressCallback = Callable[[int, str], None]
_WHISPER_PROGRESS_PATTERN = re.compile(r"progress\s*=\s*(\d{1,3})%")


def _run(command: list[str], *, timeout: int, operation: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise TranscriptionError(f"{operation} executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise TranscriptionError(f"{operation} timed out after {timeout} seconds") from exc
    except OSError as exc:
        raise TranscriptionError(f"Could not start {operation}: {exc}") from exc
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "unknown error").strip()
        raise TranscriptionError(
            f"{operation} exited with code {completed.returncode}: {details[-10000:]}"
        )
    return completed


def _run_with_progress(
    command: list[str],
    *,
    timeout: int,
    operation: str,
    progress_callback: ProgressCallback,
) -> None:
    """Run a command and stream whisper.cpp progress without pipe deadlocks."""
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise TranscriptionError(f"{operation} executable was not found") from exc
    except OSError as exc:
        raise TranscriptionError(f"Could not start {operation}: {exc}") from exc

    output_queue: queue.Queue[str | None] = queue.Queue()

    def read_stderr() -> None:
        assert process.stderr is not None
        try:
            for line in process.stderr:
                output_queue.put(line)
        finally:
            process.stderr.close()
            output_queue.put(None)

    reader = threading.Thread(target=read_stderr, daemon=True)
    reader.start()
    started_at = time.monotonic()
    output_tail = ""
    stream_finished = False
    try:
        while process.poll() is None or not stream_finished:
            if time.monotonic() - started_at > timeout:
                process.kill()
                process.wait()
                raise TranscriptionError(
                    f"{operation} timed out after {timeout} seconds"
                )
            try:
                line = output_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                stream_finished = True
                continue
            output_tail = (output_tail + line)[-10000:]
            match = _WHISPER_PROGRESS_PATTERN.search(line)
            if match:
                whisper_percent = min(100, int(match.group(1)))
                progress_callback(5 + round(whisper_percent * 0.95), "Whisper识别")
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        reader.join(timeout=1)

    if process.returncode != 0:
        details = output_tail.strip() or "unknown error"
        raise TranscriptionError(
            f"{operation} exited with code {process.returncode}: {details}"
        )


def _atomic_write(path: Path, content: str) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(content, encoding="utf-8")
    os.replace(temporary_path, path)


def transcribe_audio(
    audio_path: Path,
    *,
    video_id: int,
    channel_name: str,
    video_title: str | None = None,
    progress_callback: ProgressCallback | None = None,
) -> TranscriptionResult:
    settings = get_settings()
    audio_path = audio_path.expanduser().resolve()
    if not audio_path.is_file():
        raise TranscriptionError(f"Audio file does not exist: {audio_path}")
    if not settings.whisper_cpp_binary.is_file():
        raise TranscriptionError(
            f"whisper.cpp executable does not exist: {settings.whisper_cpp_binary}"
        )
    if not os.access(settings.whisper_cpp_binary, os.X_OK):
        raise TranscriptionError(
            f"whisper.cpp executable is not executable: {settings.whisper_cpp_binary}"
        )
    if not settings.whisper_model_path.is_file():
        raise TranscriptionError(
            f"Whisper model does not exist: {settings.whisper_model_path}"
        )

    transcript_root = settings.transcript_dir.resolve()
    output_name = safe_directory_name(
        video_title or audio_path.stem,
        fallback=f"video-{video_id}",
        max_length=180,
    )
    output_directory = (
        settings.transcript_dir
        / safe_channel_directory_name(channel_name)
        / output_name
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    output_directory = output_directory.resolve()
    if not output_directory.is_relative_to(transcript_root):
        raise TranscriptionError("Transcript output directory is outside the configured root")

    with tempfile.TemporaryDirectory(prefix=f"youtube-transcription-{video_id}-") as temp_dir:
        temp_root = Path(temp_dir)
        wav_path = temp_root / "input.wav"
        whisper_output = temp_root / "whisper-output"
        if progress_callback:
            progress_callback(0, "准备音频")
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(audio_path),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(wav_path),
            ],
            timeout=settings.transcription_timeout_seconds,
            operation="ffmpeg",
        )
        if progress_callback:
            progress_callback(5, "Whisper识别")
        whisper_command = [
            str(settings.whisper_cpp_binary),
            "-m",
            str(settings.whisper_model_path),
            "-l",
            settings.whisper_language,
            "-t",
            str(settings.whisper_threads),
            "--beam-size",
            "1",
            "--split-on-word",
            "--max-len",
            "1",
            "--dtw",
            settings.whisper_model,
            "--output-json-full",
            "--output-file",
            str(whisper_output),
            "--file",
            str(wav_path),
        ]
        if progress_callback:
            whisper_command.append("--print-progress")
            _run_with_progress(
                whisper_command,
                timeout=settings.transcription_timeout_seconds,
                operation="whisper.cpp",
                progress_callback=progress_callback,
            )
        else:
            _run(
                whisper_command,
                timeout=settings.transcription_timeout_seconds,
                operation="whisper.cpp",
            )
        raw_json_source = whisper_output.with_suffix(".json")
        if not raw_json_source.is_file():
            raise TranscriptionError("whisper.cpp completed without producing JSON output")
        # whisper.cpp can emit token strings that split a multi-byte UTF-8
        # character when word-level timestamps are enabled. Replacement keeps
        # the JSON valid and preserves the usable segment-level transcription.
        raw_text = raw_json_source.read_bytes().decode("utf-8", errors="replace")
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise TranscriptionError("whisper.cpp produced invalid JSON") from exc
        captions = whisper_json_to_captions(raw)

    raw_json_path = output_directory / "whisper-raw.json"
    captions_path = output_directory / "captions.json"
    srt_path = output_directory / "transcript.srt"
    transcript_path = output_directory+".md"
    text = transcript_text(captions)
    _atomic_write(raw_json_path, raw_text)
    _atomic_write(
        captions_path,
        json.dumps(captions_as_dicts(captions), ensure_ascii=False, indent=2),
    )
    _atomic_write(srt_path, serialize_srt(captions))
    _atomic_write(transcript_path, text + "\n")
    if progress_callback:
        progress_callback(100, "完成")
    return TranscriptionResult(
        transcript_text=text,
        transcript_path=transcript_path,
        captions_path=captions_path,
        srt_path=srt_path,
        raw_json_path=raw_json_path,
    )
