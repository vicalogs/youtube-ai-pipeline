from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.transcriber import _run_with_progress, transcribe_audio


class TranscriberTests(unittest.TestCase):
    def test_streams_native_whisper_progress(self) -> None:
        updates: list[tuple[int, str]] = []
        _run_with_progress(
            [
                sys.executable,
                "-c",
                "import sys; print('progress = 42%', file=sys.stderr)",
            ],
            timeout=5,
            operation="progress-test",
            progress_callback=lambda percent, phase: updates.append((percent, phase)),
        )
        self.assertEqual(updates, [(45, "Whisper识别")])

    def test_runs_ffmpeg_and_whisper_and_writes_compatible_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "audio.mp3"
            binary = root / "whisper-cli"
            model = root / "ggml-small.bin"
            audio.touch()
            binary.touch()
            binary.chmod(0o755)
            model.touch()
            settings = Settings(
                database_url="sqlite://",
                download_dir=root / "downloads",
                log_file=root / "app.log",
                scheduler_interval_minutes=30,
                max_retries=3,
                download_timeout_seconds=10,
                log_level="INFO",
                whisper_cpp_binary=binary,
                whisper_model_path=model,
                transcript_dir=root / "transcripts",
                transcription_timeout_seconds=30,
            )
            raw = {
                "transcription": [
                    {
                        "text": " 测试文字",
                        "offsets": {"from": 0, "to": 1200},
                        "tokens": [{"p": 0.95, "t_dtw": 5}],
                    }
                ]
            }
            calls: list[tuple[list[str], dict]] = []

            def fake_run(command, **kwargs):
                calls.append((command, kwargs))
                if command[0] == "ffmpeg":
                    Path(command[-1]).touch()
                else:
                    output = Path(command[command.index("--output-file") + 1])
                    payload = json.dumps(raw, ensure_ascii=False).encode("utf-8")
                    # Some whisper.cpp token strings contain incomplete UTF-8
                    # byte sequences even though the segment text is valid.
                    payload = payload.replace(
                        b'{"transcription"',
                        b'{"invalid_token": "\xe5\xff", "transcription"',
                    )
                    output.with_suffix(".json").write_bytes(payload)
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch("app.transcriber.get_settings", return_value=settings),
                patch("app.transcriber.subprocess.run", side_effect=fake_run),
            ):
                result = transcribe_audio(
                    audio,
                    video_id=7,
                    channel_name="老蛮频道",
                    video_title='测试/标题:第一期',
                )

            # The whisper payload carries an incomplete UTF-8 sequence, so a
            # readable transcript also proves lenient decoding still works.
            self.assertEqual(result.transcript_text, "测试文字")
            self.assertTrue(result.transcript_path.is_file())
            self.assertEqual(result.transcript_path.name, "audio.md")
            markdown = result.transcript_path.read_text(encoding="utf-8")
            self.assertTrue(markdown.startswith("# 测试/标题:第一期\n"))
            self.assertIn("`audio.mp3`", markdown)
            self.assertIn("00:00:01", markdown)
            self.assertTrue(markdown.rstrip().endswith("测试文字"))
            # transcripts/<频道>/<音频名>.md, with no per-video subdirectory.
            self.assertEqual(result.transcript_path.parent.name, "老蛮频道")
            self.assertEqual(
                result.transcript_path.parent.parent, (root / "transcripts").resolve()
            )
            self.assertNotIn("video-7", str(result.transcript_path))
            self.assertEqual(
                [item.name for item in result.transcript_path.parent.iterdir()],
                ["audio.md"],
            )
            whisper_command = calls[1][0]
            self.assertIn("--output-json-full", whisper_command)
            # Without this prompt whisper.cpp returns Chinese with no punctuation.
            self.assertEqual(
                whisper_command[whisper_command.index("--prompt") + 1],
                "以下是普通话的句子。",
            )
            # Word-level segmentation stripped sentence punctuation, and nothing
            # consumes token timestamps now that only Markdown is produced.
            for removed in ("--split-on-word", "--max-len", "--dtw"):
                self.assertNotIn(removed, whisper_command)
            self.assertNotIn("shell", calls[0][1])
            self.assertNotIn("shell", calls[1][1])


if __name__ == "__main__":
    unittest.main()
