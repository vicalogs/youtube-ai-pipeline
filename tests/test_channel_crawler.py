from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.channel_crawler import (
    CrawledVideo,
    _parse_video,
    crawl_channel,
    normalize_channel_url,
)


class ChannelCrawlerTests(unittest.TestCase):
    def test_normalizes_channel_and_removes_tab(self) -> None:
        self.assertEqual(
            normalize_channel_url("https://youtube.com/@finance/videos?view=0"),
            "https://www.youtube.com/@finance",
        )

    def test_rejects_video_url(self) -> None:
        with self.assertRaises(ValueError):
            normalize_channel_url("https://www.youtube.com/watch?v=abc123")

    def test_parses_required_metadata(self) -> None:
        video = _parse_video(
            json.dumps(
                {
                    "id": "abc123",
                    "title": "Market Update",
                    "view_count": 4567,
                    "webpage_url": "https://www.youtube.com/watch?v=abc123",
                    "thumbnail": "https://i.ytimg.com/example.jpg",
                    "upload_date": "20260729",
                    "playlist_index": 1,
                    "playlist_count": 100,
                }
            ),
            "videos",
        )
        assert video is not None
        self.assertEqual(video.title, "Market Update")
        self.assertEqual(video.view_count, 4567)
        self.assertEqual(video.published_at.year, 2026)
        self.assertEqual(video.playlist_index, 1)
        self.assertEqual(video.playlist_count, 100)

    def test_deduplicates_videos_across_tabs(self) -> None:
        normal = CrawledVideo(
            youtube_id="abc123",
            youtube_url="https://www.youtube.com/watch?v=abc123",
            title="One",
            view_count=1,
            thumbnail_url=None,
            published_at=None,
            source_tab="videos",
        )
        short = CrawledVideo(
            youtube_id="abc123",
            youtube_url="https://www.youtube.com/shorts/abc123",
            title="One",
            view_count=2,
            thumbnail_url=None,
            published_at=None,
            source_tab="shorts",
        )

        def fake_tab(_url, tab, _max_videos=None):
            yield normal if tab == "videos" else short

        with patch("app.channel_crawler.crawl_channel_tab", side_effect=fake_tab):
            videos = list(crawl_channel("https://youtube.com/@finance", ("videos", "shorts")))
        self.assertEqual(videos, [normal])


if __name__ == "__main__":
    unittest.main()
