from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pika

from a500_t_trader.market_data import Trade
from a500_t_trader.market_data.rabbitmq import RabbitMQPublisher

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "compose.yaml")]
URL = "amqp://guest:guest@localhost:5672/%2F"


def main() -> None:
    environment = {**os.environ, "A500_RABBITMQ_URL": URL}
    subprocess.run([*COMPOSE, "up", "-d", "--build", "--wait"], cwd=ROOT, env=environment, check=True)
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
            queue = f"market.consumer.{purpose}"
            while True:
                try:
                    connection.channel().queue_declare(queue=queue, passive=True)
                    break
                except pika.exceptions.ChannelClosedByBroker:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"consumer queue {queue} was not declared")
                    time.sleep(1)
        publisher = RabbitMQPublisher(url=URL)
        try:
            publisher.publish(Trade("563360", datetime.now(timezone.utc), price=Decimal("1.25"), quantity=100, amount=Decimal("125"), source="compose-e2e"))
        finally:
            publisher.close()
            connection.close()
        for purpose in ("signals", "audit"):
            output = ""
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                result = subprocess.run([*COMPOSE, "logs", "--no-log-prefix", "--since=1m", f"consumer-{purpose}"], cwd=ROOT, env=environment, text=True, capture_output=True, check=True)
                output = result.stdout
                events = []
                for line in output.splitlines():
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict) and item.get("source") == "compose-e2e":
                        events.append(item)
                if events:
                    break
                time.sleep(1)
            if not any(item.get("event_type") == "trade" and item.get("symbol") == "563360" and item.get("data", {}).get("quantity") == 100 for item in events):
                raise AssertionError(f"schema-valid event was not routed to {purpose}: {output}")
    finally:
        subprocess.run([*COMPOSE, "down", "--volumes", "--remove-orphans"], cwd=ROOT, env=environment, check=True)


if __name__ == "__main__":
    main()
