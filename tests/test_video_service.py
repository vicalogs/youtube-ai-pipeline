from __future__ import annotations

import unittest

from app.services.video_service import validate_youtube_url


class VideoUrlValidationTests(unittest.TestCase):
    def test_accepts_watch_short_and_shorts_urls(self) -> None:
        urls = [
            "https://www.youtube.com/watch?v=abc123",
            "https://youtu.be/abc123",
            "https://youtube.com/shorts/abc123",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(validate_youtube_url(url), url)

    def test_rejects_non_youtube_url(self) -> None:
        with self.assertRaises(ValueError):
            validate_youtube_url("https://example.com/watch?v=abc123")


if __name__ == "__main__":
    unittest.main()

