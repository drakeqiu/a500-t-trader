from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pika

from a500_t_trader.market_data import MinuteBar, Snapshot, Trade
from a500_t_trader.market_data.consumer import RabbitMQConsumer
from a500_t_trader.market_data.rabbitmq import RabbitMQPublisher

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "compose.yaml")]
URL = "amqp://guest:guest@localhost:5672/%2F"


def main() -> None:
    environment = {**os.environ, "A500_RABBITMQ_URL": URL}
    subprocess.run(
        [*COMPOSE, "up", "-d", "--build", "--wait"],
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
            queues = (
                f"market.consumer.{purpose}.snapshot.563360",
                f"market.consumer.{purpose}.minute_bar.563360",
                f"market.consumer.{purpose}.trade",
            )
            for queue in queues:
                deadline = time.monotonic() + 60
                while True:
                    try:
                        connection.channel().queue_declare(queue=queue, passive=True)
                        break
                    except pika.exceptions.ChannelClosedByBroker:
                        if time.monotonic() >= deadline:
                            raise RuntimeError(
                                f"consumer queue {queue} was not declared"
                            ) from None
                        time.sleep(1)
        publisher = RabbitMQPublisher(url=URL)
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
        _verify_latest_state_backlog(connection)
        connection.close()
        for purpose in ("signals", "audit"):
            output = ""
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                result = subprocess.run(
                    [
                        *COMPOSE,
                        "logs",
                        "--no-log-prefix",
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
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        isinstance(item, dict)
                        and item.get("data", {}).get("source") == "compose-e2e"
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


def _verify_latest_state_backlog(connection: pika.BlockingConnection) -> None:
    queue_channel = connection.channel()
    consumer = RabbitMQConsumer(
        "backlog-check",
        lambda event: None,
        symbols=("563360", "510300"),
        channel=queue_channel,
    )
    consumer.configure()
    publisher = RabbitMQPublisher(url=URL)
    try:
        now = datetime.now(UTC)
        publisher.publish(
            Snapshot(
                "563360", now, now, last_price=Decimal("1.10"), source="old-snapshot"
            )
        )
        publisher.publish(
            Snapshot(
                "563360", now, now, last_price=Decimal("1.20"), source="latest-snapshot"
            )
        )
        publisher.publish(
            Snapshot(
                "510300", now, now, last_price=Decimal("1.30"), source="other-symbol"
            )
        )
        publisher.publish(
            MinuteBar(
                "563360",
                now,
                1,
                Decimal("1.00"),
                Decimal("1.20"),
                Decimal("0.90"),
                Decimal("1.10"),
                1,
                source="old-bar",
            )
        )
        publisher.publish(
            MinuteBar(
                "563360",
                now,
                1,
                Decimal("1.10"),
                Decimal("1.30"),
                Decimal("1.00"),
                Decimal("1.20"),
                2,
                source="latest-bar",
            )
        )
        for quantity in (1, 2):
            publisher.publish(
                Trade(
                    "563360",
                    now,
                    price=Decimal("1.25"),
                    quantity=quantity,
                    amount=Decimal("1.25") * quantity,
                    source="backlog-trade",
                )
            )
    finally:
        publisher.close()

    expected_latest = {
        "market.consumer.backlog-check.snapshot.563360": "latest-snapshot",
        "market.consumer.backlog-check.snapshot.510300": "other-symbol",
        "market.consumer.backlog-check.minute_bar.563360": "latest-bar",
    }
    for queue, expected_source in expected_latest.items():
        state = queue_channel.queue_declare(queue=queue, passive=True).method
        if state.message_count != 1:
            raise AssertionError(f"{queue} retained {state.message_count} messages")
        method, _, body = queue_channel.basic_get(queue=queue, auto_ack=True)
        if method is None or json.loads(body)["data"]["source"] != expected_source:
            raise AssertionError(f"{queue} did not retain {expected_source}")
    trade_queue = "market.consumer.backlog-check.trade"
    trade_state = queue_channel.queue_declare(queue=trade_queue, passive=True).method
    if trade_state.message_count != 2:
        raise AssertionError("latest-state coalescing changed the trade backlog")


if __name__ == "__main__":
    main()
