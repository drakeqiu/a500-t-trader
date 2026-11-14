#!/usr/bin/env python3
"""Expose the development stack over plain HTTP at a specified IPv4 address."""

import argparse
import ipaddress
import json
import runpy
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
LOCAL = runpy.run_path(str(ROOT / "scripts/start-local.py"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "host", help="host machine's IPv4 address reachable by the intended clients"
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=("up", "ps", "logs", "down", "replay"),
        default="up",
    )
    parser.add_argument("--timeout", type=int, default=300)
    args, extra = parser.parse_known_args()
    if extra and args.action != "replay":
        parser.error("only replay accepts additional arguments")
    try:
        host = ipaddress.IPv4Address(args.host)
        if host.is_loopback or host.is_unspecified or host.is_multicast:
            raise ValueError(
                "host must be a LAN IPv4 address, not loopback or a wildcard"
            )
        env = LOCAL["local_environment"]()
        env.update(MARKET_DATA_MODE="replay")
        LOCAL["check_configuration"](env)
        origin = f"http://{host}:8080"
        issuer = f"http://{host}:8081/auth/v1/"
        env.update(
            GATEWAY_HOST="0.0.0.0",
            GATEWAY_HOST_PORT="8080",
            RAUTHY_HOST="0.0.0.0",
            RAUTHY_HOST_PORT="8081",
            RAUTHY_PUB_URL=f"{host}:8081",
            RAUTHY_RP_ID=str(host),
            RAUTHY_RP_ORIGIN=f"http://{host}:8081",
            RAUTHY_BROWSER_ORIGIN=origin,
            OIDC_ISSUER=issuer,
            OIDC_JWKS_URI="http://rauthy:8080/auth/v1/oidc/certs",
            RABBITMQ_BIND_HOST="127.0.0.1",
        )
        env["COMPOSE_PROJECT_NAME"] = "a500-lan"
        LOCAL["prepare_rauthy_bootstrap"](env)
        command = LOCAL["compose_command"](env)
        if args.action == "up":
            subprocess.run(
                ["uv", "build", "--wheel"],
                cwd=ROOT,
                env=env,
                check=True,
            )
            subprocess.run(
                [*command, "up", "-d", "--build", *LOCAL["SERVICES"]],
                cwd=ROOT,
                env=env,
                check=True,
            )
            deadline = time.monotonic() + args.timeout
            discovery_url = (
                "http://127.0.0.1:8081/auth/v1/.well-known/openid-configuration"
            )
            while True:
                try:
                    with urlopen(discovery_url, timeout=2) as response:
                        if json.load(response).get("issuer") == env["OIDC_ISSUER"]:
                            break
                except OSError, URLError, ValueError:
                    pass
                if time.monotonic() >= deadline:
                    raise ValueError("Rauthy readiness timed out; inspect logs")
                time.sleep(1)
            for path, expected in (
                ("/", 200),
                ("/api/v1/signals", 401),
                ("/api/v1/account-snapshot", 401),
            ):
                while True:
                    try:
                        with urlopen(
                            f"http://127.0.0.1:8080{path}", timeout=2
                        ) as response:
                            status = response.status
                    except HTTPError as error:
                        status = error.code
                    except OSError:
                        status = 0
                    if status == expected:
                        break
                    if time.monotonic() >= deadline:
                        raise ValueError(f"Gateway readiness timed out: {path}")
                    time.sleep(1)
            LOCAL["set_client_audience"](env)
            LOCAL["set_browser_origin"](env)
            print(f"Ready: {origin} (Rauthy: {issuer})")
            print(
                "HTTP provides no confidentiality or integrity; use only with "
                "accepted risk."
            )
        elif args.action == "replay":
            subprocess.run(
                [
                    *command,
                    "--profile",
                    "replay",
                    "run",
                    "--rm",
                    "replay",
                    "a500-market-data-replay",
                    *extra,
                ],
                cwd=ROOT,
                env=env,
                check=True,
            )
        else:
            subprocess.run(
                [
                    *command,
                    args.action,
                    *(["--tail", "100"] if args.action == "logs" else []),
                ],
                cwd=ROOT,
                env=env,
                check=True,
            )
    except (
        ValueError,
        OSError,
        subprocess.CalledProcessError,
        StopIteration,
        json.JSONDecodeError,
    ) as error:
        if isinstance(error, subprocess.CalledProcessError):
            print(
                "LAN startup failed: container command failed; inspect logs",
                file=sys.stderr,
            )
        else:
            print(f"LAN startup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
