from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.main import build_parser, run_command
from app.transcriber import TranscriptionResult


class MainCommandTests(unittest.TestCase):
    def test_transcribe_audio_accepts_a_direct_path_without_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio_path = root / "节目.mp3"
            audio_path.touch()
            result = TranscriptionResult(
                transcript_text="测试文字",
                transcript_path=root / "transcript.txt",
                captions_path=root / "captions.json",
                srt_path=root / "transcript.srt",
                raw_json_path=root / "whisper-raw.json",
            )
            args = build_parser().parse_args(
                [
                    "transcribe-audio",
                    "--audio-path",
                    str(audio_path),
                    "--channel-name",
                    "测试频道",
                    "--title",
                    "测试标题",
                ]
            )
            output = io.StringIO()
            with (
                patch("app.main.transcribe_audio", return_value=result) as transcribe,
                patch("app.main.check_database_connection") as check_database,
                redirect_stdout(output),
            ):
                exit_code = run_command(args)

            self.assertEqual(exit_code, 0)
            check_database.assert_not_called()
            transcribe.assert_called_once_with(
                audio_path.resolve(),
                video_id=0,
                channel_name="测试频道",
                video_title="测试标题",
            )
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["transcript_path"], str(result.transcript_path))


if __name__ == "__main__":
    unittest.main()
