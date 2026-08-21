from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    Channel,
    Transcription,
    TranscriptionStatus,
    Video,
    VideoStatus,
)
from app.services.transcription_service import (
    enqueue_completed_transcriptions,
    prepare_video_transcription,
    retry_failed_transcription,
)


class TranscriptionQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_backfills_completed_downloads_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "audio.mp3"
            audio_path.touch()
            with Session(self.engine) as session:
                channel = Channel(
                    name="老蛮频道",
                    url="https://www.youtube.com/@finance",
                )
                session.add(channel)
                session.flush()
                video = Video(
                    channel_id=channel.id,
                    youtube_url="https://www.youtube.com/watch?v=abc123",
                    title="测试影片",
                    status=VideoStatus.COMPLETED.value,
                    file_path=str(audio_path),
                )
                session.add(video)
                session.commit()

            with Session(self.engine) as session:
                tasks = enqueue_completed_transcriptions(session)
                session.commit()
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0].status, TranscriptionStatus.PENDING.value)

            with Session(self.engine) as session:
                self.assertEqual(enqueue_completed_transcriptions(session), [])
                stored = session.scalar(select(Transcription))
                assert stored is not None
                self.assertEqual(stored.video.title, "测试影片")

    def test_explicitly_retries_an_exhausted_failed_task(self) -> None:
        with Session(self.engine) as session:
            channel = Channel(name="频道", url="https://youtube.com/@channel")
            session.add(channel)
            session.flush()
            video = Video(
                channel_id=channel.id,
                youtube_url="https://www.youtube.com/watch?v=retry1",
                status=VideoStatus.COMPLETED.value,
            )
            session.add(video)
            session.flush()
            task = Transcription(
                video_id=video.id,
                status=TranscriptionStatus.FAILED.value,
                retry_count=3,
                error_message="old decoder error",
            )
            session.add(task)
            session.commit()
            task_id = task.id

        with Session(self.engine) as session:
            retried = retry_failed_transcription(session, task_id)
            self.assertEqual(retried.status, TranscriptionStatus.PENDING.value)
            self.assertEqual(retried.retry_count, 0)
            self.assertIsNone(retried.error_message)

    def test_prepares_a_downloaded_video_for_immediate_transcription(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "selected.mp3"
            audio_path.touch()
            with Session(self.engine) as session:
                channel = Channel(
                    name="指定频道", url="https://youtube.com/@selected"
                )
                session.add(channel)
                session.flush()
                video = Video(
                    channel_id=channel.id,
                    youtube_url="https://www.youtube.com/watch?v=selected1",
                    title="指定视频",
                    status=VideoStatus.COMPLETED.value,
                    file_path=str(audio_path),
                )
                session.add(video)
                session.commit()
                video_id = video.id

            with Session(self.engine) as session:
                task = prepare_video_transcription(session, video_id)
                self.assertEqual(
                    task.status, TranscriptionStatus.TRANSCRIBING.value
                )
                self.assertEqual(task.video_id, video_id)
                self.assertEqual(task.retry_count, 0)


if __name__ == "__main__":
    unittest.main()
