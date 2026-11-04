from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pika

from a500_t_trader.market_data import Trade
from a500_t_trader.market_data.rabbitmq import RabbitMQPublisher

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = [
    *shlex.split(os.environ.get("A500_COMPOSE_COMMAND", "docker compose")),
    "-f",
    str(ROOT / "compose.yaml"),
]
URL = "amqp://guest:guest@localhost:5672/%2F"


def main() -> None:
    environment = {
        **os.environ,
        "A500_RABBITMQ_URL": URL,
        "A500_MARKET_DATA_MODE": "tgw",
    }
    subprocess.run(
        [*COMPOSE, "up", "-d", "--build"],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    try:
        connection = None
        for _ in range(60):
            try:
                connection = pika.BlockingConnection(pika.URLParameters(URL))
                break
            except pika.exceptions.AMQPConnectionError:
                time.sleep(1)
        if connection is None:
            raise RuntimeError("RabbitMQ did not become available")
        for purpose in ("signals", "audit"):
            deadline = time.monotonic() + 60
            queue = f"market.consumer.{purpose}.tgw"
            while True:
                try:
                    connection.channel().queue_declare(queue=queue, passive=True)
                    break
                except pika.exceptions.ChannelClosedByBroker as error:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"consumer queue {queue} was not declared"
                        ) from error
                    time.sleep(1)
        publisher = RabbitMQPublisher(url=URL, exchange="market.events.tgw")
        try:
            publisher.publish(
                Trade(
                    "563360",
                    datetime.now(UTC),
                    price=Decimal("1.25"),
                    quantity=100,
                    amount=Decimal("125"),
                    source="compose-e2e",
                )
            )
        finally:
            publisher.close()
            connection.close()
        for purpose in ("signals", "audit"):
            output = ""
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                result = subprocess.run(
                    [
                        *COMPOSE,
                        "logs",
                        "--since=1m",
                        f"consumer-{purpose}",
                    ],
                    cwd=ROOT,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                output = result.stdout
                events = []
                for line in output.splitlines():
                    start = line.find("{")
                    if start < 0:
                        continue
                    try:
                        item = json.loads(line[start:])
                    except json.JSONDecodeError:
                        continue
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("data"), dict)
                        and item["data"].get("source") == "compose-e2e"
                    ):
                        events.append(item)
                if events:
                    break
                time.sleep(1)
            if not any(
                item.get("event_type") == "trade"
                and item.get("symbol") == "563360"
                and item.get("data", {}).get("quantity") == 100
                for item in events
            ):
                raise AssertionError(
                    f"schema-valid event was not routed to {purpose}: {output}"
                )
    finally:
        subprocess.run(
            [*COMPOSE, "down", "--volumes", "--remove-orphans"],
            cwd=ROOT,
            env=environment,
            check=True,
        )


if __name__ == "__main__":
    main()
