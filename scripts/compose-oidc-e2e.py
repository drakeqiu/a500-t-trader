from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


class JwksHandler(BaseHTTPRequestHandler):
    keys: list[dict[str, object]] = []

    def do_GET(self) -> None:
        if self.path != "/jwks":
            self.send_error(404)
            return
        body = json.dumps({"keys": self.keys}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/register":
            self.send_error(404)
            return
        self.keys = [json.loads(self.rfile.read(int(self.headers["Content-Length"]))) ]
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def main() -> None:
    compose = shlex.split(
        os.environ.get(
            "A500_COMPOSE_COMMAND",
            "docker compose" if shutil.which("docker") else "podman compose",
        )
    )
    identity_port = free_port()
    gateway_port = free_port()
    jwks = HTTPServer(("0.0.0.0", identity_port), JwksHandler)
    threading.Thread(target=jwks.serve_forever, daemon=True).start()
    identity_host = (
        "host.docker.internal" if compose[0] == "docker" else "host.containers.internal"
    )
    issuer = "http://identity.example.test"
    environment = {
        **os.environ,
        "A500_OIDC_ISSUER": issuer,
        "A500_OIDC_AUDIENCE": "a500-api",
        "A500_OIDC_CLIENT_ID": "a500-web-client",
        "A500_OIDC_JWKS_URI": f"http://{identity_host}:{identity_port}/jwks",
        "A500_MARKET_DATA_MODE": "replay",
        "AMAZINGDATA_USERNAME": "deployment-test",
        "AMAZINGDATA_PASSWORD": "deployment-test",
        "AMAZINGDATA_HOST": "127.0.0.1",
        "AMAZINGDATA_PORT": "1",
        "A500_E2E_ISSUER": issuer,
        "A500_E2E_JWKS_REGISTER_URL": f"http://127.0.0.1:{identity_port}/register",
        "A500_E2E_GATEWAY_URL": f"http://127.0.0.1:{gateway_port}",
    }
    project = f"issue145-{os.getpid()}"
    with tempfile.TemporaryDirectory(prefix="a500-oidc-e2e-") as temporary:
        override = Path(temporary) / "compose-override.yaml"
        override_text = "services:\n"
        if identity_host == "host.docker.internal":
            override_text += (
                "  signal-api:\n"
                "    extra_hosts:\n"
                "      - 'host.docker.internal:host-gateway'\n"
            )
        override_text += (
            "  account-api:\n"
            + (
                "    extra_hosts:\n"
                "      - 'host.docker.internal:host-gateway'\n"
                if identity_host == "host.docker.internal"
                else ""
            )
            + "    command: [python, -c, \"import uvicorn; from "
            "a500_t_trader.account_snapshot_api import create_app; "
            "uvicorn.run(create_app(), host='0.0.0.0', port=8000)\"]\n"
            f"  gateway:\n    ports:\n      - '127.0.0.1:{gateway_port}:8080'\n"
        )
        override.write_text(override_text, encoding="utf-8")
        _assert_missing_oidc_fails(compose, override, environment, temporary)
        compose_args = [
            *compose,
            "-p",
            project,
            "-f",
            str(ROOT / "compose.yaml"),
            "-f",
            str(override),
        ]
        try:
            subprocess.run(
                [*compose_args, "up", "-d", "--build", "redis", "signal-api", "account-api", "gateway"],
                cwd=ROOT,
                env=environment,
                check=True,
                timeout=600,
            )
            _wait_for_gateway(
                compose_args, environment["A500_E2E_GATEWAY_URL"], environment
            )
            _probe_internal_health(compose_args, environment)
            _probe_external_boundaries(compose_args, environment)
            subprocess.run(
                ["npx", "playwright", "test", "tests/browser/deployment-oidc.spec.js"],
                cwd=ROOT,
                env=environment,
                check=True,
                timeout=180,
            )
        finally:
            subprocess.run(
                [*compose_args, "down", "--volumes", "--remove-orphans"],
                cwd=ROOT,
                env=environment,
                check=False,
                timeout=60,
            )
            jwks.shutdown()


def _wait_for_gateway(
    compose_args: list[str], url: str, environment: dict[str, str]
) -> None:
    deadline = time.monotonic() + 90
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{url}/web-config", timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, URLError) as error:
            last_error = error
        time.sleep(1)
    diagnostics: list[str] = []
    for service in ("gateway", "signal-api", "account-api"):
        result = subprocess.run(
            [*compose_args, "logs", service],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        diagnostics.append(
            f"{service}:\n" + "\n".join(result.stdout.splitlines()[-30:])
        )
    raise RuntimeError(
        f"OIDC gateway did not become ready: {last_error}\n" + "\n".join(diagnostics)
    )


def _probe_internal_health(compose_args: list[str], environment: dict[str, str]) -> None:
    subprocess.run(
        [
            *compose_args,
            "exec",
            "-T",
            "signal-api",
            "python",
            "-c",
            "from urllib.request import urlopen; assert urlopen('http://127.0.0.1:8000/healthz').status == 200",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        timeout=15,
    )


def _assert_missing_oidc_fails(
    compose: list[str],
    override: Path,
    environment: dict[str, str],
    temporary: str,
) -> None:
    missing_oidc = environment.copy()
    for variable in (
        "A500_OIDC_ISSUER",
        "A500_OIDC_AUDIENCE",
        "A500_OIDC_JWKS_URI",
        "A500_OIDC_CLIENT_ID",
    ):
        missing_oidc.pop(variable, None)
    empty_env = Path(temporary) / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    result = subprocess.run(
        [
            *compose,
            "--env-file",
            str(empty_env),
            "-f",
            str(ROOT / "compose.yaml"),
            "-f",
            str(override),
            "config",
            "--services",
        ],
        cwd=ROOT,
        env=missing_oidc,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        raise AssertionError("Compose must fail when unified OIDC settings are missing")
def _probe_external_boundaries(
    compose_args: list[str], environment: dict[str, str]
) -> None:
    try:
        urlopen(f"{environment['A500_E2E_GATEWAY_URL']}/healthz", timeout=3)
    except URLError as error:
        if getattr(error, "code", None) != 404:
            raise AssertionError(
                f"gateway healthz must not be externally available: {error}"
            ) from error
    else:
        raise AssertionError("gateway must not expose /healthz")

    for service in ("signal-api", "account-api"):
        result = subprocess.run(
            [*compose_args, "port", service, "8000"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            raise AssertionError(f"{service}:8000 must not be published on the host")


if __name__ == "__main__":
    main()
