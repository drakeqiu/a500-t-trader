"""Durable RabbitMQ dispatch for daily-history jobs."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

from .database import database_url
from .jobs import JobRepository

_LOG = logging.getLogger(__name__)
HISTORY_EXCHANGE = "daily-history.jobs"
HISTORY_QUEUE = "daily-history.worker"


class HistoryPublisher:
    """Publisher for durable daily-history dispatch messages."""

    def __init__(self, *, url: str | None = None, channel: Any | None = None) -> None:
        self._connection: Any | None = None
        self._properties: Any | None = None
        if channel is None:
            import pika  # type: ignore[import-untyped]

            rabbit_url = url or os.environ.get("A500_RABBITMQ_URL")
            if not rabbit_url:
                raise ValueError("A500_RABBITMQ_URL is required")
            self._connection = pika.BlockingConnection(pika.URLParameters(rabbit_url))
            channel = self._connection.channel()
            self._properties = pika.BasicProperties(
                content_type="application/json", delivery_mode=2
            )
        self._channel = channel
        channel.exchange_declare(
            exchange=HISTORY_EXCHANGE, exchange_type="direct", durable=True
        )
        channel.queue_declare(queue=HISTORY_QUEUE, durable=True)
        channel.queue_bind(
            queue=HISTORY_QUEUE, exchange=HISTORY_EXCHANGE, routing_key="job"
        )
        channel.confirm_delivery()

    def publish(self, job_id: str) -> None:
        import pika  # type: ignore[import-untyped]

        confirmed = self._channel.basic_publish(
            exchange=HISTORY_EXCHANGE,
            routing_key="job",
            body=json.dumps({"job_id": job_id}, separators=(",", ":")).encode(),
            properties=self._properties
            or pika.BasicProperties(content_type="application/json", delivery_mode=2),
            mandatory=True,
        )
        if confirmed is False:
            raise RuntimeError("RabbitMQ did not confirm history dispatch")

    def close(self) -> None:
        if self._connection is not None and self._connection.is_open:
            self._connection.close()

class HistoryConsumer:
    """RabbitMQ worker；任务状态仍由 PostgreSQL 决定。"""

    def __init__(
        self,
        repository: JobRepository,
        *,
        url: str | None = None,
        channel: Any | None = None,
        provider_factory: Any | None = None,
        retention_days: int = 30,
    ) -> None:
        self.repository = repository
        self.retention_days = retention_days
        self._provider_factory = provider_factory
        self._last_cleanup = 0.0
        self._connection: Any | None = None
        if channel is None:
            import pika  # type: ignore[import-untyped]

            rabbit_url = url or os.environ.get("A500_RABBITMQ_URL")
            if not rabbit_url:
                raise ValueError("A500_RABBITMQ_URL is required")
            self._connection = pika.BlockingConnection(pika.URLParameters(rabbit_url))
            channel = self._connection.channel()
        self._channel = channel
        channel.exchange_declare(
            exchange=HISTORY_EXCHANGE, exchange_type="direct", durable=True
        )
        channel.queue_declare(queue=HISTORY_QUEUE, durable=True)
        channel.queue_bind(
            queue=HISTORY_QUEUE, exchange=HISTORY_EXCHANGE, routing_key="job"
        )
        channel.basic_qos(prefetch_count=1)

    def _process(self, job_id: str) -> None:
        from .jobs import process_job

        if self._provider_factory is None:
            process_job(self.repository, job_id)
        else:
            process_job(self.repository, job_id, self._provider_factory)

    def _on_message(
        self, channel: Any, method: Any, properties: Any, body: bytes
    ) -> None:
        try:
            message = json.loads(body)
            job_id = message["job_id"]
            if not isinstance(job_id, str):
                raise ValueError("job_id must be a string")
            job_id = str(uuid.UUID(job_id))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            channel.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
            return
        try:
            self._process(job_id)
        except Exception:
            _LOG.exception("History notification processing failed; requeueing")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        else:
            # 已完成、租约仍有效或其他 worker 已领取时均安全确认。
            channel.basic_ack(delivery_tag=method.delivery_tag)

    def _recover_expired(self) -> None:
        try:
            for job_id in self.repository.expired_job_ids():
                self._process(str(job_id))
            if time.monotonic() - self._last_cleanup >= 3600:
                self.repository.cleanup(self.retention_days)
                self._last_cleanup = time.monotonic()
        except Exception:
            _LOG.exception("History lease recovery failed")
        finally:
            self._channel.connection.call_later(30, self._recover_expired)

    def run(self) -> None:
        self._channel.basic_consume(
            queue=HISTORY_QUEUE,
            on_message_callback=self._on_message,
            auto_ack=False,
        )
        # 补偿已确认投递后 worker 崩溃的任务；只扫描租约已过期的运行任务。
        self._channel.connection.call_later(30, self._recover_expired)
        self._channel.start_consuming()

    def close(self) -> None:
        if self._connection is not None and self._connection.is_open:
            self._connection.close()


def publisher_main() -> None:
    repository = JobRepository(database_url())
    while True:
        publisher: HistoryPublisher | None = None
        try:
            publisher = HistoryPublisher()
            pending = repository.outbox_backlog()
            _LOG.info(
                "History dispatch backlog pending=%s ready=%s oldest=%s",
                pending["pending"], pending["ready"], pending["oldest_created_at"],
            )
            count = repository.publish_pending(publisher)
            if count == 0:
                time.sleep(2)
        except Exception:
            _LOG.exception("History dispatch failed; outbox entries remain retryable")
            time.sleep(2)
        finally:
            if publisher is not None:
                publisher.close()


if __name__ == "__main__":
    publisher_main()
