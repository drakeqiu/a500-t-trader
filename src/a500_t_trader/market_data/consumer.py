"""RabbitMQ consumer for normalized market-data JSON events."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

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
    """Consume purpose-specific latest-state queues and a competing trade queue."""

    def __init__(
        self,
        purpose: str,
        handler: Handler,
        *,
        symbols: tuple[str, ...],
        url: str | None = None,
        exchange: str = "market.events",
        channel: Any | None = None,
        max_age: timedelta = timedelta(seconds=30),
    ) -> None:
        self.consumer = MarketEventConsumer(purpose, handler, max_age=max_age)
        allowed_symbol_chars = (
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
        )
        if not symbols or any(
            not symbol or any(char not in allowed_symbol_chars for char in symbol)
            for symbol in symbols
        ):
            raise ValueError("symbols must be non-empty routing-key-safe strings")
        symbols = tuple(dict.fromkeys(symbols))
        prefix = f"market.consumer.{purpose}"
        self.trade_queue = f"{prefix}.trade"
        self.latest_queues = tuple(
            (
                f"{prefix}.{event_type}.{quote(symbol, safe='')}",
                f"{event_type}.{quote(symbol, safe='')}",
            )
            for event_type in ("snapshot", "minute_bar")
            for symbol in symbols
        )
        self.queues = tuple(
            queue for queue, _ in self.latest_queues
        ) + (self.trade_queue,)
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
        state_arguments = {"x-max-length": 1, "x-overflow": "drop-head"}
        for queue, routing_key in self.latest_queues:
            self._channel.queue_declare(
                queue=queue, durable=True, arguments=state_arguments
            )
            self._channel.queue_bind(
                queue=queue, exchange=self.exchange, routing_key=routing_key
            )
        self._channel.queue_declare(queue=self.trade_queue, durable=True)
        self._channel.queue_bind(
            queue=self.trade_queue,
            exchange=self.exchange,
            routing_key="trade.*",
        )
        self._channel.basic_qos(prefetch_count=1)

    def run(self) -> None:
        self.configure()
        for queue in self.queues:
            self._channel.basic_consume(
                queue=queue,
                on_message_callback=self._handle_delivery,
                auto_ack=False,
            )
        self._channel.start_consuming()

    def _handle_delivery(
        self, channel: Any, method: Any, properties: Any, body: bytes
    ) -> None:
        self.consumer.handle_delivery(channel, method.delivery_tag, body)

    def stop(self) -> None:
        self._channel.stop_consuming()

    def close(self) -> None:
        if self._connection is not None and self._connection.is_open:
            self._connection.close()
