"""Start the real local browser/API/Keycloak topology without storing credentials."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SERVICES = (
    "keycloak",
    "rabbitmq",
    "redis",
    "consumer-signals",
    "consumer-audit",
    "signal-calculator",
    "signal-api",
    "account-api",
    "gateway",
    "frontend",
)


def local_environment() -> dict[str, str]:
    """Read literal dotenv values, never execute the user's configuration file."""
    environment: dict[str, str] = {}
    dotenv = ROOT / ".env"
    if dotenv.exists():
        for number, line in enumerate(dotenv.read_text().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or not key.isidentifier():
                raise ValueError(f".env line {number}: expected NAME=literal-value")
            try:
                tokens = shlex.split(value, comments=True)
            except ValueError:
                raise ValueError(f".env line {number}: invalid quoting") from None
            if len(tokens) > 1:
                raise ValueError(f".env line {number}: quote values containing spaces")
            environment[key] = tokens[0] if tokens else ""
    environment.update(os.environ)
    for suffix in ("USERNAME", "PASSWORD", "HOST", "PORT"):
        target = f"AMAZINGDATA_{suffix}"
        if not environment.get(target):
            environment[target] = environment.get(f"AD_{suffix}", "")
    environment.setdefault("A500_OIDC_ISSUER", "http://localhost:8081/realms/a500")
    environment.setdefault("A500_OIDC_AUDIENCE", "a500-api")
    environment.setdefault(
        "A500_OIDC_JWKS_URI",
        "http://keycloak:8080/realms/a500/protocol/openid-connect/certs",
    )
    environment.setdefault("KEYCLOAK_HOSTNAME", "http://localhost:8081")
    # Compose must use the sanitized literal environment, not parse .env again.
    return environment


def compose_command(environment: dict[str, str]) -> list[str]:
    configured = environment.get("A500_COMPOSE_COMMAND") or environment.get(
        "PODMAN_COMPOSE"
    )
    if configured:
        command = shlex.split(configured)
    elif (
        shutil.which("docker")
        and subprocess.run(
            ["docker", "compose", "version"], capture_output=True, check=False
        ).returncode
        == 0
    ):
        command = ["docker", "compose"]
    elif shutil.which("podman-compose"):
        command = ["podman-compose"]
    else:
        raise ValueError("Install Docker Compose or podman + podman-compose")
    if not command or not shutil.which(command[0]):
        raise ValueError("Configured Compose executable is unavailable")
    engine = "podman" if "podman" in command[0] else "docker"
    if (
        not shutil.which(engine)
        or subprocess.run([engine, "info"], capture_output=True, check=False).returncode
    ):
        raise ValueError(f"{engine} is unavailable; check its daemon/rootless setup")
    return [
        *command,
        "--env-file",
        os.devnull,
        "--project-name",
        "a500-local",
        "-f",
        str(ROOT / "compose.yaml"),
        "-f",
        str(ROOT / "compose.keycloak.yaml"),
        "-f",
        str(ROOT / "compose.web.yaml"),
        "-f",
        str(ROOT / "compose.web-http.yaml"),
    ]


def check_configuration(environment: dict[str, str]) -> None:
    required = (
        "AMAZINGDATA_USERNAME",
        "AMAZINGDATA_PASSWORD",
        "AMAZINGDATA_HOST",
        "AMAZINGDATA_PORT",
        "KEYCLOAK_ADMIN_USERNAME",
        "KEYCLOAK_ADMIN_PASSWORD",
        "A500_POSTGRES_PASSWORD",
    )
    missing = [name for name in required if not environment.get(name)]
    if missing:
        raise ValueError("Missing settings: " + ", ".join(missing))
    if environment.get("A500_MARKET_DATA_MODE") not in {"tgw", "replay"}:
        raise ValueError("Set A500_MARKET_DATA_MODE explicitly to tgw or replay")
    try:
        port = int(environment["AMAZINGDATA_PORT"])
    except ValueError:
        raise ValueError("AMAZINGDATA_PORT must be an integer") from None
    if not 1 <= port <= 65535:
        raise ValueError("AMAZINGDATA_PORT must be between 1 and 65535")
    expected = {
        "A500_OIDC_ISSUER": "http://localhost:8081/realms/a500",
        "A500_OIDC_AUDIENCE": "a500-api",
        "A500_OIDC_JWKS_URI": (
            "http://keycloak:8080/realms/a500/protocol/openid-connect/certs"
        ),
        "KEYCLOAK_HOSTNAME": "http://localhost:8081",
        "KEYCLOAK_HOST_PORT": "8081",
    }
    for name, value in expected.items():
        if environment.get(name, value) != value:
            raise ValueError(
                f"{name} conflicts with the local browser topology; "
                "use raw Compose for a custom origin/IdP"
            )
    for wheel in (
        "AmazingData-1.1.9-cp314-none-any.whl",
        "tgw-1.0.9.2-py3-none-any.whl",
    ):
        if not (ROOT / "wheels" / wheel).is_file():
            raise ValueError(f"Missing wheels/{wheel}; obtain the deps-v1 release")


def wait_ready(timeout: int, gateway_port: str = "8080") -> None:
    gateway = f"http://localhost:{gateway_port}"
    endpoints = {
        "Keycloak discovery": (
            "http://localhost:8081/realms/a500/.well-known/openid-configuration",
            200,
        ),
        "frontend": (gateway, 200),
        "signal API proxy": (f"{gateway}/api/v1/signals", 401),
        "account API proxy": (f"{gateway}/api/v1/account-snapshot", 401),
    }
    deadline = time.monotonic() + timeout
    pending = dict(endpoints)
    while pending and time.monotonic() < deadline:
        for name, (url, expected) in list(pending.items()):
            try:
                with urlopen(url, timeout=2) as response:
                    status = response.status
                    if name == "Keycloak discovery":
                        discovery = json.load(response)
                        if discovery.get("issuer") != (
                            "http://localhost:8081/realms/a500"
                        ):
                            continue
            except HTTPError as error:
                status = error.code
            except OSError, URLError, ValueError:
                continue
            if status == expected:
                del pending[name]
        if pending:
            time.sleep(1)
    if pending:
        raise ValueError(
            "Readiness timed out: "
            + ", ".join(pending)
            + ". Inspect containers with: python3 scripts/start-local.py logs"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        choices=("up", "down", "ps", "logs", "stop-frontend", "replay"),
        default="up",
    )
    parser.add_argument(
        "--timeout", type=int, default=180, help="Readiness timeout in seconds"
    )
    arguments, replay_arguments = parser.parse_known_args()
    if replay_arguments and arguments.action != "replay":
        parser.error("Unexpected arguments; only replay accepts backend CLI options")
    try:
        environment = local_environment()
        check_configuration(environment)
        command = compose_command(environment)
        if arguments.action == "up":
            subprocess.run(
                [*command, "up", "-d", "--build", *SERVICES],
                cwd=ROOT,
                env=environment,
                check=True,
            )
            gateway_port = environment.get("A500_GATEWAY_HOST_PORT", "8080")
            wait_ready(arguments.timeout, gateway_port)
            print(
                f"Ready: http://localhost:{gateway_port} (Keycloak: http://localhost:8081)"
            )
            print(
                "Provision an enabled user in the a500 realm; no default user exists."
            )
            print("Market mode: " + environment["A500_MARKET_DATA_MODE"])
        elif arguments.action == "stop-frontend":
            subprocess.run(
                [*command, "stop", "frontend"],
                cwd=ROOT,
                env=environment,
                check=True,
            )
        elif arguments.action == "replay":
            if environment["A500_MARKET_DATA_MODE"] != "replay":
                raise ValueError("Replay requires A500_MARKET_DATA_MODE=replay")
            subprocess.run(
                [
                    *command,
                    "--profile",
                    "replay",
                    "run",
                    "--rm",
                    "replay",
                    "a500-market-data-replay",
                    *replay_arguments,
                ],
                cwd=ROOT,
                env=environment,
                check=True,
            )
        else:
            extra = ["--tail", "100"] if arguments.action == "logs" else []
            subprocess.run(
                [*command, arguments.action, *extra],
                cwd=ROOT,
                env=environment,
                check=True,
            )
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        # Never include configuration contents, child environments, or credentials.
        if isinstance(error, ValueError):
            print(str(error), file=sys.stderr)
        else:
            print(
                "Container command failed; check prerequisites and service logs.",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
