from __future__ import annotations

import unittest

from app.captions import (
    build_caption_pages,
    serialize_srt,
    transcript_text,
    whisper_json_to_captions,
)


class CaptionCompatibilityTests(unittest.TestCase):
    def raw(self):
        return {
            "transcription": [
                {
                    "text": " 第一段",
                    "offsets": {"from": 0, "to": 1000},
                    "tokens": [{"p": 0.9, "t_dtw": 12}],
                },
                {
                    "text": " English",
                    "offsets": {"from": 1100, "to": 2000},
                    "tokens": [{"p": 0.8, "t_dtw": -1}],
                },
                {
                    "text": "第二页。",
                    "offsets": {"from": 3000, "to": 4000},
                    "tokens": [{"p": 0.7, "t_dtw": 300}],
                },
            ]
        }

    def test_matches_remotion_caption_fields(self) -> None:
        captions = whisper_json_to_captions(self.raw())
        self.assertEqual(captions[0].text, "第一段")
        self.assertEqual(captions[0].startMs, 0)
        self.assertEqual(captions[0].endMs, 1000)
        self.assertEqual(captions[0].timestampMs, 120)
        self.assertEqual(captions[0].confidence, 0.9)
        self.assertIsNone(captions[1].timestampMs)

    def test_pages_on_long_pause_and_serializes_srt(self) -> None:
        captions = whisper_json_to_captions(self.raw())
        pages = build_caption_pages(captions)
        self.assertEqual(len(pages), 2)
        self.assertEqual(transcript_text(captions), "第一段 English第二页。")
        srt = serialize_srt(captions)
        self.assertIn("00:00:00,000 --> 00:00:02,000", srt)
        self.assertIn("第一段 English", srt)
        self.assertIn("00:00:03,000 --> 00:00:04,000", srt)


if __name__ == "__main__":
    unittest.main()

