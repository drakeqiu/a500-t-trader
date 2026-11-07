from __future__ import annotations

import json

import psycopg

from a500_t_trader.history_dispatch import (
    HISTORY_EXCHANGE,
    HISTORY_QUEUE,
    HistoryPublisher,
)
from a500_t_trader.jobs import JobRepository


def test_confirmed_dispatch_marks_outbox_published(postgres_url: str) -> None:
    repository = JobRepository(postgres_url)
    job = repository.create("alice", "563360", "2020-01-01", "2020-12-31")

    class Channel:
        def __init__(self) -> None:
            self.declarations: list[tuple[str, str]] = []
            self.messages: list[tuple[str, str, dict[str, str]]] = []

        def exchange_declare(self, **kwargs: str) -> None:
            self.declarations.append(("exchange", kwargs["exchange"]))

        def queue_declare(self, **kwargs: str) -> None:
            self.declarations.append(("queue", kwargs["queue"]))

        def queue_bind(self, **kwargs: str) -> None:
            self.declarations.append(("binding", kwargs["exchange"]))

        def confirm_delivery(self) -> None:
            pass

        def basic_publish(self, **kwargs: object) -> bool:
            self.messages.append((
                str(kwargs["exchange"]), str(kwargs["routing_key"]),
                json.loads(kwargs["body"]),
            ))
            return True

    channel = Channel()
    publisher = HistoryPublisher(channel=channel)
    assert repository.publish_pending(publisher) == 1
    assert channel.declarations == [
        ("exchange", HISTORY_EXCHANGE), ("queue", HISTORY_QUEUE),
        ("binding", HISTORY_EXCHANGE),
    ]
    assert channel.messages == [(HISTORY_EXCHANGE, "job", {"job_id": job["id"]})]
    with psycopg.connect(postgres_url) as db:
        row = db.execute(
            "SELECT published_at, attempts, last_error FROM daily_history_job_outbox "
            "WHERE job_id=%s", (job["id"],),
        ).fetchone()
    assert row is not None and row[0] is not None and row[1:] == (1, None)
    assert repository.outbox_backlog()["pending"] == 0


def test_failed_dispatch_remains_pending_and_backlog_visible(postgres_url: str) -> None:
    repository = JobRepository(postgres_url)
    job = repository.create("alice", "563360", "2020-01-01", "2020-12-31")

    class UnavailablePublisher:
        def publish(self, job_id: str) -> None:
            raise RuntimeError("broker unavailable")

    assert repository.publish_pending(UnavailablePublisher()) == 1
    with psycopg.connect(postgres_url) as db:
        row = db.execute(
            "SELECT published_at, attempts, last_error, available_at FROM "
            "daily_history_job_outbox WHERE job_id=%s", (job["id"],),
        ).fetchone()
    assert row is not None
    assert row[0] is None and row[1] == 1 and row[2] == "broker unavailable"
    assert row[3] is not None
    backlog = repository.outbox_backlog()
    assert backlog["pending"] == 1
    assert backlog["oldest_created_at"] is not None


def test_repository_startup_backfills_legacy_queued_jobs(postgres_url: str) -> None:
    repository = JobRepository(postgres_url)
    with psycopg.connect(postgres_url) as db:
        job_id = db.execute(
            """INSERT INTO daily_history_jobs
            (id, subject, symbol, start_date, end_date, status, total_chunks)
            VALUES (gen_random_uuid(), 'legacy', '563360', '2020-01-01',
                '2020-12-31', 'queued', 1) RETURNING id"""
        ).fetchone()[0]

    repository = JobRepository(postgres_url)

    class RecordingPublisher:
        def __init__(self) -> None:
            self.job_ids: list[str] = []

        def publish(self, job_id: str) -> None:
            self.job_ids.append(job_id)

    publisher = RecordingPublisher()
    assert repository.publish_pending(publisher) == 1
    assert publisher.job_ids == [str(job_id)]


def test_consumer_processes_only_notified_job_and_acks_duplicate(
    postgres_url: str,
) -> None:
    from types import SimpleNamespace

    from a500_t_trader.history_dispatch import HistoryConsumer
    from a500_t_trader.market_data import DailyBar

    repository = JobRepository(postgres_url)
    notified = repository.create("alice", "563360", "2020-01-01", "2020-12-31")
    unrelated = repository.create("alice", "563361", "2020-01-01", "2020-12-31")

    class Provider:
        def get_daily_history(
            self, symbol: str, start: object, end: object
        ) -> tuple[DailyBar, ...]:
            return ()

    class Channel:
        def __init__(self) -> None:
            self.acks: list[int] = []
            self.nacks: list[int] = []

        def exchange_declare(self, **kwargs: object) -> None:
            pass

        def queue_declare(self, **kwargs: object) -> None:
            pass

        def queue_bind(self, **kwargs: object) -> None:
            pass

        def basic_qos(self, **kwargs: object) -> None:
            pass

        def basic_ack(self, *, delivery_tag: int) -> None:
            self.acks.append(delivery_tag)

        def basic_nack(self, *, delivery_tag: int, requeue: bool) -> None:
            self.nacks.append(delivery_tag)

        def basic_reject(self, *, delivery_tag: int, requeue: bool) -> None:
            self.nacks.append(delivery_tag)

    held_lease = repository.claim(unrelated["id"])
    assert held_lease is not None
    channel = Channel()
    consumer = HistoryConsumer(repository, channel=channel, provider_factory=Provider)
    consumer._on_message(
        channel, SimpleNamespace(delivery_tag=1), None,
        json.dumps({"job_id": notified["id"]}).encode(),
    )
    consumer._on_message(
        channel, SimpleNamespace(delivery_tag=2), None,
        json.dumps({"job_id": notified["id"]}).encode(),
    )
    consumer._on_message(
        channel, SimpleNamespace(delivery_tag=3), None,
        json.dumps({"job_id": unrelated["id"]}).encode(),
    )

    assert channel.acks == [1, 2, 3]
    assert channel.nacks == []
    with psycopg.connect(postgres_url) as db:
        statuses = db.execute(
            "SELECT id, status FROM daily_history_jobs WHERE id IN (%s, %s)",
            (notified["id"], unrelated["id"]),
        ).fetchall()

    assert {str(identifier): status for identifier, status in statuses} == {
        notified["id"]: "succeeded",
        unrelated["id"]: "running",
    }


def test_running_job_without_lease_is_recovered_as_legacy_state(
    postgres_url: str,
) -> None:
    repository = JobRepository(postgres_url)
    job = repository.create("alice", "563360", "2020-01-01", "2020-12-31")
    first_lease = repository.claim(job["id"])
    assert first_lease is not None

    with psycopg.connect(postgres_url) as db:
        db.execute(
            "UPDATE daily_history_jobs SET lease_until=NULL WHERE id=%s",
            (job["id"],),
        )

    assert job["id"] in [str(identifier) for identifier in repository.expired_job_ids()]
    recovered = repository.claim(job["id"])
    assert recovered is not None
    assert recovered["lease_token"] != first_lease["lease_token"]
