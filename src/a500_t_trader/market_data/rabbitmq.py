"""AMQP publisher for versioned market-data events."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote

from .events import MarketEvent, encode_market_event


class RabbitMQPublisher:
    """Publish market events to a RabbitMQ topic exchange over AMQP."""

    def __init__(
        self,
        *,
        url: str | None = None,
        exchange: str = "market.events",
        channel: Any | None = None,
    ) -> None:
        self._exchange = exchange
        self._connection: Any | None = None
        self._properties: Any | None = None
        if channel is None:
            import pika  # type: ignore[import-untyped]

            rabbit_url = url or os.environ.get("A500_RABBITMQ_URL")
            if not rabbit_url:
                raise ValueError("A500_RABBITMQ_URL is required")
            self._connection = pika.BlockingConnection(
                pika.URLParameters(rabbit_url)
            )
            channel = self._connection.channel()
            channel.exchange_declare(
                exchange=exchange, exchange_type="topic", durable=True
            )
            self._properties = pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,
            )
        self._channel = channel
        confirm_delivery = getattr(channel, "confirm_delivery", None)
        if callable(confirm_delivery):
            confirm_delivery()

    def publish(self, event: MarketEvent) -> None:
        encoded = encode_market_event(event)
        self._channel.basic_publish(
            exchange=self._exchange,
            routing_key=(
                f"{encoded['event_type']}."
                f"{quote(str(encoded['symbol']), safe='')}"
            ),
            body=json.dumps(
                encoded, separators=(",", ":"), ensure_ascii=False
            ).encode(),
            properties=self._properties,
            mandatory=True,
        )

    def close(self) -> None:
        if self._connection is not None and self._connection.is_open:
            self._connection.close()

