from __future__ import annotations

import json
import os
import runpy
import secrets
import shlex
import socket
import subprocess
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pika

from a500_t_trader.market_data import Trade
from a500_t_trader.market_data.rabbitmq import RabbitMQPublisher

ROOT = Path(__file__).resolve().parents[1]
LOCAL = runpy.run_path(str(ROOT / "scripts" / "start-local.py"))
COMPOSE = [
    *shlex.split(os.environ.get("COMPOSE_COMMAND", "docker compose")),
    "-f",
    str(ROOT / "compose.yaml"),
]


def _project_name() -> str:
    return f"a500-market-data-e2e-{secrets.token_hex(6)}"


def _consumer_image(project: str) -> str:
    return f"{project}-consumer:local"


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _published_port(compose: list[str], environment: dict[str, str]) -> int:
    if "podman" in Path(compose[0]).name:
        project = compose[compose.index("--project-name") + 1]
        container = subprocess.run(
            [
                "podman",
                "ps",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                "label=com.docker.compose.service=rabbitmq",
            ],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not container or "\n" in container:
            raise RuntimeError("无法唯一确定 rabbitmq 容器")
        result = subprocess.run(
            [
                "podman",
                "inspect",
                "--format",
                "{{json .NetworkSettings.Ports}}",
                container,
            ],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        bindings = json.loads(result.stdout).get("5672/tcp") or []
        if len(bindings) != 1:
            raise RuntimeError(f"rabbitmq 端口绑定不唯一: {bindings!r}")
        return int(bindings[0]["HostPort"])
    result = subprocess.run(
        [*compose, "port", "rabbitmq", "5672"],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip().rsplit(":", 1)[1])


def _compose_command(project: str) -> list[str]:
    return [
        *COMPOSE[:-2],
        "--project-name",
        project,
        *COMPOSE[-2:],
    ]


def main() -> None:
    project = _project_name()
    consumer_image = _consumer_image(project)
    environment = LOCAL["configure_local_build_environment"](
        {
            **os.environ,
            "MARKET_DATA_MODE": "tgw",
            "MARKET_DATA_CONSUMER_IMAGE": consumer_image,
            "RABBITMQ_HOST_PORT": str(_available_port()),
            "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
            "POSTGRES_PORT": str(_available_port()),
            "OIDC_ISSUER": "https://identity.example.test",
            "OIDC_AUDIENCE": "a500-api",
            "OIDC_JWKS_URI": "https://identity.example.test/jwks",
            "AD_USERNAME": "market-data-e2e",
            "AD_PASSWORD": "market-data-e2e",
            "AD_HOST": "127.0.0.1",
            "AD_PORT": "1",
        }
    )
    compose = _compose_command(project)
    try:
        subprocess.run(
            ["uv", "build", "--wheel"],
            cwd=ROOT,
            env=environment,
            check=True,
        )
        subprocess.run(
            [*compose, "up", "-d", "rabbitmq", "redis"],
            cwd=ROOT,
            env=environment,
            check=True,
            timeout=300,
        )
        port = _published_port(compose, environment)
        url = f"amqp://guest:guest@localhost:{port}/%2F"
        environment["RABBITMQ_URL"] = url
        subprocess.run(
            [
                *compose,
                "up",
                "-d",
                "--build",
                "signal-calculator",
                "consumer-signals",
                "consumer-audit",
            ],
            cwd=ROOT,
            env=environment,
            check=True,
            timeout=600,
        )

        connection = None
        for _ in range(60):
            try:
                connection = pika.BlockingConnection(pika.URLParameters(url))
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
        publisher = RabbitMQPublisher(url=url, exchange="market.events.tgw")
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
                        *compose,
                        "logs",
                        "--since=1m",
                        f"consumer-{purpose}",
                    ],
                    cwd=ROOT,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=True,
                    timeout=15,
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

    except subprocess.CalledProcessError:
        subprocess.run(
            [*compose, "logs", "rabbitmq"],
            cwd=ROOT,
            env=environment,
            check=False,
            timeout=15,
        )
        raise

    finally:
        subprocess.run(
            [*compose, "down", "--volumes", "--remove-orphans"],
            cwd=ROOT,
            env=environment,
            check=True,
            timeout=60,
        )


if __name__ == "__main__":
    main()
