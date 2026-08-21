from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, VideoStatus
from app.services.video_service import DuplicateVideoError, add_video_task, list_videos


class ModelAndTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_add_and_list_pending_video(self) -> None:
        with Session(self.engine) as session:
            video = add_video_task(
                session,
                youtube_url="https://www.youtube.com/watch?v=abc123",
                channel_name="Finance Channel",
                channel_url="https://www.youtube.com/@finance",
                category="财经",
            )
            session.commit()
            video_id = video.id

        with Session(self.engine) as session:
            videos = list_videos(session, status=VideoStatus.PENDING.value)
            self.assertEqual(len(videos), 1)
            self.assertEqual(videos[0].id, video_id)
            self.assertEqual(videos[0].channel.category, "财经")
            self.assertEqual(videos[0].download_logs[0].message, "Task created")

    def test_duplicate_video_is_rejected(self) -> None:
        task = {
            "youtube_url": "https://youtu.be/abc123",
            "channel_name": "Finance Channel",
            "channel_url": "https://www.youtube.com/@finance",
        }
        with Session(self.engine) as session:
            add_video_task(session, **task)
            session.commit()
            with self.assertRaises(DuplicateVideoError):
                add_video_task(session, **task)


if __name__ == "__main__":
    unittest.main()

