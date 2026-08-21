from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.downloader import (
    DownloadError,
    download_audio,
    download_file_storage_value,
    resolve_download_file_path,
    safe_channel_directory_name,
)


class DownloaderTests(unittest.TestCase):
    def make_settings(self, directory: Path) -> Settings:
        return Settings(
            database_url="postgresql+psycopg2://unused",
            download_dir=directory,
            log_file=directory / "app.log",
            scheduler_interval_minutes=30,
            max_retries=3,
            download_timeout_seconds=10,
            log_level="INFO",
        )

    def test_download_uses_argument_list_and_returns_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            download_dir = Path(temp_dir)
            channel_dir = download_dir / "老蛮频道"
            channel_dir.mkdir()
            audio_file = channel_dir / "Example.mp3"
            audio_file.touch()
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=f"{audio_file}\n", stderr=""
            )

            with (
                patch("app.downloader.get_settings", return_value=self.make_settings(download_dir)),
                patch("app.downloader.subprocess.run", return_value=completed) as run,
            ):
                result = download_audio(
                    "https://www.youtube.com/watch?v=abc123",
                    channel_name="老蛮频道",
                )

            self.assertEqual(result.file_path, audio_file.resolve())
            command = run.call_args.args[0]
            self.assertIsInstance(command, list)
            self.assertEqual(command[0], "yt-dlp")
            self.assertNotIn("shell", run.call_args.kwargs)
            output_template = command[command.index("-o") + 1]
            self.assertIn("老蛮频道", output_template)

    def test_channel_directory_name_blocks_path_traversal(self) -> None:
        self.assertEqual(safe_channel_directory_name("../../财经/频道"), "_.._财经_频道")
        self.assertEqual(safe_channel_directory_name(".."), "unknown-channel")

    def test_download_path_is_portable_and_recovers_old_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            download_dir = Path(temp_dir) / "audio" / "downloads"
            audio_file = download_dir / "频道" / "节目.mp3"
            audio_file.parent.mkdir(parents=True)
            audio_file.touch()
            settings = self.make_settings(download_dir)
            old_path = Path("/old/project/audio/downloads/频道/节目.mp3")

            with patch("app.downloader.get_settings", return_value=settings):
                self.assertEqual(
                    download_file_storage_value(audio_file), "频道/节目.mp3"
                )
                self.assertEqual(
                    resolve_download_file_path("频道/节目.mp3"), audio_file.resolve()
                )
                self.assertEqual(
                    resolve_download_file_path(old_path), audio_file.resolve()
                )

    def test_nonzero_exit_becomes_download_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self.make_settings(Path(temp_dir))
            completed = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="network unavailable"
            )
            with (
                patch("app.downloader.get_settings", return_value=settings),
                patch("app.downloader.subprocess.run", return_value=completed),
            ):
                with self.assertRaisesRegex(DownloadError, "network unavailable"):
                    download_audio("https://www.youtube.com/watch?v=abc123")


if __name__ == "__main__":
    unittest.main()
