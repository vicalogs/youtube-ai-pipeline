from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.channel_crawler import CrawledVideo
from app.models import Base, Channel, ChannelCrawlState, ChannelVideo, VideoStatus
from app.services.channel_service import (
    configure_channel_schedule,
    enqueue_channel_video,
    process_channel_batch,
    upsert_channel_videos,
)
from app.services.download_service import enqueue_catalog_videos


class ChannelServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            channel = Channel(
                name="Finance",
                url="https://www.youtube.com/@finance",
                category="财经",
            )
            session.add(channel)
            session.commit()
            self.channel_id = channel.id

    def tearDown(self) -> None:
        self.engine.dispose()

    def item(self, *, title: str, view_count: int) -> CrawledVideo:
        return CrawledVideo(
            youtube_id="abc123",
            youtube_url="https://www.youtube.com/watch?v=abc123",
            title=title,
            view_count=view_count,
            thumbnail_url="https://i.ytimg.com/abc123.jpg",
            published_at=None,
            source_tab="videos",
        )

    @contextmanager
    def local_session_scope(self):
        session = Session(self.engine)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def test_upsert_inserts_then_updates_metadata(self) -> None:
        with Session(self.engine) as session:
            self.assertEqual(
                upsert_channel_videos(
                    session, self.channel_id, [self.item(title="Old", view_count=10)]
                ),
                (1, 0),
            )
            session.commit()

        with Session(self.engine) as session:
            self.assertEqual(
                upsert_channel_videos(
                    session, self.channel_id, [self.item(title="New", view_count=20)]
                ),
                (0, 1),
            )
            session.commit()
            stored = session.scalar(select(ChannelVideo))
            assert stored is not None
            self.assertEqual(stored.title, "New")
            self.assertEqual(stored.view_count, 20)

    def test_catalog_video_can_be_promoted_to_download_task(self) -> None:
        with Session(self.engine) as session:
            upsert_channel_videos(
                session, self.channel_id, [self.item(title="Download Me", view_count=10)]
            )
            session.commit()

        with Session(self.engine) as session:
            task = enqueue_channel_video(session, "abc123")
            session.commit()
            self.assertEqual(task.status, VideoStatus.PENDING.value)
            self.assertEqual(task.title, "Download Me")
            self.assertEqual(task.download_logs[0].message.split()[0], "Task")

    def test_scheduled_batches_advance_reset_and_overwrite_duplicates(self) -> None:
        with Session(self.engine) as session:
            states = configure_channel_schedule(
                session,
                channel_url="https://www.youtube.com/@finance",
                tabs=("videos",),
                batch_size=10,
                interval_minutes=30,
            )
            session.commit()
            state_id = states[0].id

        def fake_crawl(_url, tab, max_videos=None, start_index=1):
            assert max_videos is not None
            total = 25
            stop = min(start_index + max_videos - 1, total)
            for index in range(start_index, stop + 1):
                yield CrawledVideo(
                    youtube_id=f"video-{index}",
                    youtube_url=f"https://www.youtube.com/watch?v=video-{index}",
                    title=f"Video {index}",
                    view_count=index,
                    thumbnail_url=f"https://i.ytimg.com/video-{index}.jpg",
                    published_at=None,
                    source_tab=tab,
                    playlist_index=index,
                    playlist_count=total,
                    progress_total=max_videos,
                )

        with (
            patch(
                "app.services.channel_service.session_scope",
                side_effect=self.local_session_scope,
            ),
            patch(
                "app.services.channel_service.crawl_channel_tab",
                side_effect=fake_crawl,
            ),
        ):
            first = process_channel_batch(state_id)
            second = process_channel_batch(state_id)
            third = process_channel_batch(state_id)
            duplicate = process_channel_batch(state_id)

        self.assertEqual((first.start_index, first.end_index, first.next_index), (1, 10, 11))
        self.assertEqual((second.start_index, second.end_index, second.next_index), (11, 20, 21))
        self.assertEqual((third.start_index, third.end_index, third.next_index), (21, 30, 1))
        self.assertTrue(third.completed_cycle)
        self.assertEqual((duplicate.inserted, duplicate.updated), (0, 10))
        with Session(self.engine) as session:
            state = session.get(ChannelCrawlState, state_id)
            assert state is not None
            self.assertEqual(state.next_index, 11)
            self.assertEqual(state.completed_cycles, 1)
            self.assertEqual(len(list(session.scalars(select(ChannelVideo)))), 25)

    def test_failed_batch_keeps_cursor_for_retry(self) -> None:
        with Session(self.engine) as session:
            states = configure_channel_schedule(
                session,
                channel_url="https://www.youtube.com/@finance",
                batch_size=10,
                interval_minutes=30,
            )
            session.commit()
            state_id = states[0].id

        with (
            patch(
                "app.services.channel_service.session_scope",
                side_effect=self.local_session_scope,
            ),
            patch(
                "app.services.channel_service.crawl_channel_tab",
                side_effect=RuntimeError("network failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "network failed"):
                process_channel_batch(state_id)

        with Session(self.engine) as session:
            state = session.get(ChannelCrawlState, state_id)
            assert state is not None
            self.assertEqual(state.next_index, 1)
            self.assertIn("network failed", state.last_error)

    def test_catalog_rows_are_enqueued_twenty_at_a_time_without_duplicates(self) -> None:
        items = [
            CrawledVideo(
                youtube_id=f"auto-{index}",
                youtube_url=f"https://www.youtube.com/watch?v=auto-{index}",
                title=f"Auto {index}",
                view_count=index,
                thumbnail_url=None,
                published_at=None,
                source_tab="videos",
            )
            for index in range(25)
        ]
        with Session(self.engine) as session:
            upsert_channel_videos(session, self.channel_id, items)
            session.commit()

        with Session(self.engine) as session:
            first_batch = enqueue_catalog_videos(session, limit=20)
            session.commit()
            self.assertEqual(len(first_batch), 20)
            self.assertTrue(all(task.channel_id == self.channel_id for task in first_batch))

        with Session(self.engine) as session:
            second_batch = enqueue_catalog_videos(session, limit=20)
            session.commit()
            self.assertEqual(len(second_batch), 5)

        with Session(self.engine) as session:
            self.assertEqual(enqueue_catalog_videos(session, limit=20), [])


if __name__ == "__main__":
    unittest.main()
