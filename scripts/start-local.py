"""Start the real local browser/API/Rauthy topology."""

from __future__ import annotations

import argparse
import json
import os
import secrets
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
    "rauthy",
    "mailpit",
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
    environment: dict[str, str] = os.environ.copy()
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
    environment["POSTGRES_PASSWORD"] = (
        environment.get("POSTGRES_PASSWORD") or "changeme"
    )
    environment["OIDC_ISSUER"] = environment.get(
        "OIDC_ISSUER", "http://localhost:8081/auth/v1/"
    )
    environment.setdefault("OIDC_AUDIENCE", "a500-api")
    environment.setdefault("OIDC_JWKS_URI", "http://rauthy:8080/auth/v1/oidc/certs")
    environment.setdefault("OIDC_CLIENT_ID", "a500-browser-client")
    environment.setdefault("RAUTHY_HOST_PORT", "8081")
    environment.setdefault("RAUTHY_PUB_URL", "localhost:8081")
    environment.setdefault("RAUTHY_LISTEN_SCHEME", "http")
    environment.setdefault("RAUTHY_LOCAL_TEST", "true")
    environment.setdefault("RAUTHY_BOOTSTRAP_HOST_DIR", str(ROOT / ".rauthy-bootstrap"))
    environment.setdefault("RAUTHY_RP_ID", "localhost")
    environment.setdefault(
        "RAUTHY_RP_ORIGIN",
        f"http://localhost:{environment['RAUTHY_HOST_PORT']}",
    )
    environment.setdefault("HISTORY_PERSISTENCE_PERMITTED", "true")
    configure_local_build_environment(environment)
    # Compose must use the sanitized literal environment, not parse .env again.
    return environment


def prepare_rauthy_bootstrap(environment: dict[str, str]) -> None:
    """Provision first-run Rauthy clients without exposing credentials."""
    directory = Path(environment["RAUTHY_BOOTSTRAP_HOST_DIR"])
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    password_file = directory / "admin-password"
    if password_file.exists():
        admin_password = password_file.read_text(encoding="utf-8").strip()
    else:
        admin_password = secrets.token_urlsafe(32)
        password_file.write_text(admin_password + "\n", encoding="utf-8")
        password_file.chmod(0o600)
    environment["RAUTHY_BOOTSTRAP_ADMIN_PASSWORD_PLAIN"] = admin_password

    gateway = environment.get(
        "RAUTHY_BROWSER_ORIGIN",
        f"http://localhost:{environment.get('GATEWAY_HOST_PORT', '8080')}",
    )
    clients = [
        {
            "id": "a500-browser-client",
            "name": "A500 browser",
            "secret": None,
            "enabled": True,
            "redirect_uris": [gateway + "/"],
            "allowed_origins": [gateway],
            "flows_enabled": ["authorization_code", "refresh_token"],
            "default_aud": "a500-api",
            "access_token_alg": "RS256",
            "id_token_alg": "RS256",
            "auth_code_lifetime": 60,
            "access_token_lifetime": 300,
            "scopes": ["openid", "profile", "email"],
            "default_scopes": ["openid", "profile", "email"],
            "force_mfa": False,
        },
        {
            "id": "a500-device-client",
            "name": "A500 device",
            "secret": None,
            "enabled": True,
            "redirect_uris": ["http://localhost/"],
            "allowed_origins": [],
            "flows_enabled": [
                "urn:ietf:params:oauth:grant-type:device_code"
            ],
            "default_aud": "a500-api",
            "access_token_alg": "RS256",
            "id_token_alg": "RS256",
            "access_token_lifetime": 300,
            "scopes": ["openid", "profile", "email"],
            "default_scopes": ["openid", "profile", "email"],
            "force_mfa": False,
        },
    ]
    path = directory / "clients.json"
    path.write_text(json.dumps(clients), encoding="utf-8")
    path.chmod(0o600)
    (directory / "users.json").write_text("[]", encoding="utf-8")
    (directory / "api_keys.json").write_text("[]", encoding="utf-8")


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
        str(ROOT / "compose.rauthy.yaml"),
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
    port = environment.get("RAUTHY_HOST_PORT", "8081")
    expected = {
        "OIDC_ISSUER": f"http://localhost:{port}/auth/v1/",
        "OIDC_AUDIENCE": "a500-api",
        "OIDC_JWKS_URI": "http://rauthy:8080/auth/v1/oidc/certs",
        "OIDC_CLIENT_ID": "a500-browser-client",
        "RAUTHY_PUB_URL": f"localhost:{port}",
        "RAUTHY_LISTEN_SCHEME": "http",
    }
    for name, value in expected.items():
        if environment.get(name, value) != value:
            raise ValueError(
                f"{name} conflicts with the local Rauthy browser topology; "
                "use raw Compose for a custom origin/IdP"
            )
    for wheel in (
        "AmazingData-1.1.17-cp314-none-any.whl",
        "tgw-1.0.9.2-py3-none-any.whl",
    ):
        if not (ROOT / "wheels" / wheel).is_file():
            raise ValueError(f"Missing wheels/{wheel}; obtain the deps-v1 release")


def wait_ready(
    timeout: int,
    gateway_port: str = "8080",
    issuer: str = "http://localhost:8081/auth/v1/",
) -> None:
    gateway = f"http://localhost:{gateway_port}"
    endpoints = {
        "Rauthy discovery": (issuer + ".well-known/openid-configuration", 200),
        "frontend": (gateway, 200),
        "signal API proxy": (f"{gateway}/api/v1/signals", 401),
        "account API proxy": (f"{gateway}/api/v1/account-snapshot", 401),
    }
    expected_issuer = issuer
    deadline = time.monotonic() + timeout
    pending = dict(endpoints)
    while pending and time.monotonic() < deadline:
        for name, (url, expected) in list(pending.items()):
            try:
                with urlopen(url, timeout=2) as response:
                    status = response.status
                    if name == "Rauthy discovery":
                        discovery = json.load(response)
                        if discovery.get("issuer") != expected_issuer:
                            continue
            except HTTPError as error:
                status = error.code
            except (OSError, URLError, ValueError):
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
        prepare_rauthy_bootstrap(environment)
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
            issuer = environment["OIDC_ISSUER"]
            wait_ready(arguments.timeout, gateway_port, issuer)
            print(f"Ready: http://localhost:{gateway_port} (Rauthy: {issuer})")
            print(
                "No user is created by default. Use the Rauthy admin UI to provision one."
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
