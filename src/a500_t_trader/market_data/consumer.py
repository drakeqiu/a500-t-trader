"""RabbitMQ consumer for normalized market-data JSON events."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

Handler = Callable[[dict[str, Any]], None]


class MarketEventConsumer:
    """Handle one use's events; duplicates are permitted by at-least-once delivery."""

    def __init__(
        self,
        purpose: str,
        handler: Handler,
        *,
        max_age: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        if not purpose or any(char not in allowed for char in purpose):
            raise ValueError("purpose must contain only letters, digits, '-' or '_'")
        if max_age < timedelta(0):
            raise ValueError("max_age must not be negative")
        self.purpose = purpose
        self.handler = handler
        self.max_age = max_age
        self.clock = clock or (lambda: datetime.now().astimezone())

    def handle_delivery(
        self, channel: Any, delivery_tag: int, body: bytes | str | Mapping[str, Any]
    ) -> None:
        try:
            event = json.loads(body) if isinstance(body, (bytes, str)) else dict(body)
            if not isinstance(event, dict):
                raise ValueError("market event must be a JSON object")
            _validate_event(event)
            event_time = datetime.fromisoformat(str(event["event_time"]))
            age = self.clock() - event_time
            if age <= self.max_age:
                self.handler(event)
        except Exception:
            channel.basic_nack(delivery_tag=delivery_tag, requeue=True)
            return
        channel.basic_ack(delivery_tag=delivery_tag)


def _validate_event(event: dict[str, Any]) -> None:
    if event.get("schema_version") != 1:
        raise ValueError("unsupported market event schema")
    if event.get("event_type") not in {"snapshot", "minute_bar", "trade"}:
        raise ValueError("unsupported market event type")
    if not isinstance(event.get("symbol"), str) or not isinstance(
        event.get("data"), dict
    ):
        raise ValueError("market event is missing normalized fields")
    event_time = datetime.fromisoformat(str(event.get("event_time", "")))
    if event_time.tzinfo is None or event_time.utcoffset() is None:
        raise ValueError("event_time must be timezone-aware")
    source_id = event.get("source_event_id")
    quality = event.get("source_event_id_quality")
    if source_id is not None and quality != "reliable":
        raise ValueError("source_event_id is usable only when marked reliable")


class RabbitMQConsumer:
    """Bind a consumer purpose to its own shared durable queue."""

    def __init__(
        self,
        purpose: str,
        handler: Handler,
        *,
        url: str | None = None,
        exchange: str = "market.events",
        channel: Any | None = None,
        max_age: timedelta = timedelta(seconds=30),
    ) -> None:
        self.consumer = MarketEventConsumer(purpose, handler, max_age=max_age)
        self.queue = f"market.consumer.{purpose}"
        self.exchange = exchange
        self._connection: Any | None = None
        if channel is None:
            import pika  # type: ignore[import-untyped]

            rabbit_url = url or os.environ.get("A500_RABBITMQ_URL")
            if not rabbit_url:
                raise ValueError("A500_RABBITMQ_URL is required")
            self._connection = pika.BlockingConnection(pika.URLParameters(rabbit_url))
            channel = self._connection.channel()
        self._channel = channel

    def configure(self) -> None:
        self._channel.exchange_declare(
            exchange=self.exchange, exchange_type="topic", durable=True
        )
        self._channel.queue_declare(queue=self.queue, durable=True)
        for routing_key in ("snapshot.*", "minute_bar.*", "trade.*"):
            self._channel.queue_bind(
                queue=self.queue, exchange=self.exchange, routing_key=routing_key
            )
        self._channel.basic_qos(prefetch_count=1)

    def run(self) -> None:
        self.configure()
        self._channel.basic_consume(
            queue=self.queue,
            on_message_callback=lambda channel, method, properties, body: (
                self.consumer.handle_delivery(channel, method.delivery_tag, body)
            ),
            auto_ack=False,
        )
        self._channel.start_consuming()

    def stop(self) -> None:
        self._channel.stop_consuming()

    def close(self) -> None:
        if self._connection is not None and self._connection.is_open:
            self._connection.close()
