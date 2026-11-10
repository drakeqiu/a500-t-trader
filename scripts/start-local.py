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
    "jobs-api",
    "jobs-worker",
    "history-dispatcher",
    "gateway",
    "frontend",
)


def configure_local_build_environment(environment: dict[str, str]) -> dict[str, str]:
    """Fill local APT/PyPI build settings while preserving non-empty overrides."""
    environment["APT_MIRROR"] = environment.get("APT_MIRROR") or "mirrors.bfsu.edu.cn"
    environment["UV_INDEX_URL"] = (
        environment.get("UV_INDEX_URL") or "https://mirrors.aliyun.com/pypi/simple/"
    )
    environment["UV_HTTP_TIMEOUT"] = environment.get("UV_HTTP_TIMEOUT") or "180"
    return environment


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
    environment["POSTGRES_PASSWORD"] = (
        environment.get("POSTGRES_PASSWORD") or "changeme"
    )
    environment.setdefault("OIDC_ISSUER", "http://localhost:8081/realms/a500")
    environment.setdefault("OIDC_AUDIENCE", "a500-api")
    environment.setdefault(
        "OIDC_JWKS_URI",
        "http://keycloak:8080/realms/a500/protocol/openid-connect/certs",
    )
    environment.setdefault("HISTORY_PERSISTENCE_PERMITTED", "true")
    configure_local_build_environment(environment)
    # Compose must use the sanitized literal environment, not parse .env again.
    return environment


def compose_command(environment: dict[str, str]) -> list[str]:
    configured = environment.get("COMPOSE_COMMAND") or environment.get("PODMAN_COMPOSE")
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
        "AD_USERNAME",
        "AD_PASSWORD",
        "AD_HOST",
        "AD_PORT",
        "KEYCLOAK_ADMIN_USERNAME",
        "KEYCLOAK_ADMIN_PASSWORD",
        "POSTGRES_PASSWORD",
    )
    missing = [name for name in required if not environment.get(name)]
    if missing:
        raise ValueError("Missing settings: " + ", ".join(missing))
    if environment.get("MARKET_DATA_MODE") not in {"tgw", "replay"}:
        raise ValueError("Set MARKET_DATA_MODE explicitly to tgw or replay")
    try:
        port = int(environment["AD_PORT"])
    except ValueError:
        raise ValueError("AD_PORT must be an integer") from None
    if not 1 <= port <= 65535:
        raise ValueError("AD_PORT must be between 1 and 65535")
    expected = {
        "OIDC_ISSUER": "http://localhost:8081/realms/a500",
        "OIDC_AUDIENCE": "a500-api",
        "OIDC_JWKS_URI": (
            "http://keycloak:8080/realms/a500/protocol/openid-connect/certs"
        ),
        "KEYCLOAK_HOSTNAME": "http://localhost:8081",
        "OIDC_CLIENT_ID": "a500-browser-client",
        "KEYCLOAK_HOST_PORT": "8081",
    }
    for name, value in expected.items():
        if environment.get(name, value) != value:
            raise ValueError(
                f"{name} conflicts with the local browser topology; "
                "use raw Compose for a custom origin/IdP"
            )
    for wheel in (
        "AmazingData-1.1.17-cp314-none-any.whl",
        "tgw-1.0.9.2-py3-none-any.whl",
    ):
        if not (ROOT / "wheels" / wheel).is_file():
            raise ValueError(f"Missing wheels/{wheel}; obtain the deps-v1 release")


def keycloak_admin(command: list[str], environment: dict[str, str]):
    """Return a kcadm runner authenticated against the running local Keycloak."""
    base = [*command, "exec", "-T", "keycloak", "/opt/keycloak/bin/kcadm.sh"]

    def admin(*arguments: str) -> str:
        return subprocess.run(
            [*base, *arguments],
            cwd=ROOT,
            env=environment,
            check=True,
            text=True,
            capture_output=True,
        ).stdout

    admin(
        "config",
        "credentials",
        "--server",
        "http://localhost:8080",
        "--realm",
        "master",
        "--user",
        environment["KEYCLOAK_ADMIN_USERNAME"],
        "--password",
        environment["KEYCLOAK_ADMIN_PASSWORD"],
    )
    return admin


def configure_browser_client(admin, origin: str) -> None:
    """Reconcile the browser client even when the realm was already imported."""
    realm = json.loads((ROOT / "keycloak/realm-a500.json").read_text())
    template = next(
        client
        for client in realm["clients"]
        if client["clientId"] == "a500-browser-client"
    )
    clients = json.loads(
        admin("get", "clients", "-r", "a500", "-q", "clientId=a500-browser-client")
    )
    client = next(
        (item for item in clients if item["clientId"] == template["clientId"]), None
    )
    settings = {
        key: value for key, value in template.items() if key != "protocolMappers"
    }
    settings["redirectUris"] = list(
        dict.fromkeys(
            [uri for uri in (client or {}).get("redirectUris", []) if "*" not in uri]
            + [*template["redirectUris"], f"{origin}/"]
        )
    )
    settings["webOrigins"] = list(
        dict.fromkeys(
            [
                uri
                for uri in (client or {}).get("webOrigins", [])
                if "*" not in uri and uri != "+"
            ]
            + [*template["webOrigins"], origin]
        )
    )
    settings["attributes"] = {
        **(client or {}).get("attributes", {}),
        **template["attributes"],
    }

    def assignments(values):
        arguments = []
        for key, value in values.items():
            encoded = (
                value
                if isinstance(value, str)
                else json.dumps(value, separators=(",", ":"))
            )
            arguments.extend(("-s", f"{key}={encoded}"))
        return arguments

    if client is None:
        admin("create", "clients", "-r", "a500", *assignments(settings))
        clients = json.loads(
            admin("get", "clients", "-r", "a500", "-q", "clientId=a500-browser-client")
        )
        client = next(
            item for item in clients if item["clientId"] == template["clientId"]
        )
    else:
        admin("update", f"clients/{client['id']}", "-r", "a500", *assignments(settings))

    endpoint = f"clients/{client['id']}/protocol-mappers/models"
    existing_mappers = json.loads(admin("get", endpoint, "-r", "a500"))
    mapper = template["protocolMappers"][0]
    old_mapper = next(
        (item for item in existing_mappers if item["name"] == mapper["name"]), None
    )
    if old_mapper is None:
        admin("create", endpoint, "-r", "a500", *assignments(mapper))
    else:
        admin(
            "update",
            f"{endpoint}/{old_mapper['id']}",
            "-r",
            "a500",
            *assignments(mapper),
        )


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
                ["uv", "build", "--wheel"],
                cwd=ROOT,
                env=environment,
                check=True,
            )
            subprocess.run(
                [*command, "up", "-d", "--build", *SERVICES],
                cwd=ROOT,
                env=environment,
                check=True,
            )
            gateway_port = environment.get("GATEWAY_HOST_PORT", "8080")
            wait_ready(arguments.timeout, gateway_port)
            admin = keycloak_admin(command, environment)
            configure_browser_client(admin, f"http://localhost:{gateway_port}")
            print(
                f"Ready: http://localhost:{gateway_port} (Keycloak: http://localhost:8081)"
            )
            print(
                "Provision an enabled user in the a500 realm; no default user exists."
            )
            print("Market mode: " + environment["MARKET_DATA_MODE"])
        elif arguments.action == "stop-frontend":
            subprocess.run(
                [*command, "stop", "frontend"],
                cwd=ROOT,
                env=environment,
                check=True,
            )
        elif arguments.action == "replay":
            if environment["MARKET_DATA_MODE"] != "replay":
                raise ValueError("Replay requires MARKET_DATA_MODE=replay")
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
