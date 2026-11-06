#!/usr/bin/env python3
"""Run the development stack for browsers on a trusted LAN."""

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
        "host", help="host machine's IPv4 address reachable from the LAN"
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
        env["UV_INDEX_URL"] = "https://mirrors.aliyun.com/pypi/simple/"
        env.update(
            KEYCLOAK_ADMIN_USERNAME="admin",
            KEYCLOAK_ADMIN_PASSWORD="changeme",
            A500_MARKET_DATA_MODE="replay",
        )
        # Validate the standard local prerequisites before applying the LAN topology.
        LOCAL["check_configuration"](env)
        origin = f"http://{host}:8080"
        keycloak = f"http://{host}:8081"
        env.update(
            A500_GATEWAY_HOST="0.0.0.0",
            A500_GATEWAY_HOST_PORT="8080",
            KEYCLOAK_BIND_HOST="0.0.0.0",
            KEYCLOAK_HOST_PORT="8081",
            KEYCLOAK_HOSTNAME=keycloak,
            A500_OIDC_ISSUER=f"{keycloak}/realms/a500",
            A500_RABBITMQ_BIND_HOST="127.0.0.1",
        )
        command = LOCAL["compose_command"](env)
        command[command.index("a500-local")] = "a500-lan"
        if args.action == "up":
            subprocess.run(
                [*command, "up", "-d", "--build", *LOCAL["SERVICES"]],
                cwd=ROOT,
                env=env,
                check=True,
            )
            deadline = time.monotonic() + args.timeout
            discovery_url = (
                "http://127.0.0.1:8081/realms/a500/.well-known/openid-configuration"
            )
            while True:
                try:
                    with urlopen(discovery_url, timeout=2) as response:
                        if json.load(response).get("issuer") == env["A500_OIDC_ISSUER"]:
                            break
                except OSError, URLError, ValueError:
                    pass
                if time.monotonic() >= deadline:
                    raise ValueError("Keycloak readiness timed out; inspect logs")
                time.sleep(1)
            # Update existing realms too: import only runs on first creation.
            base = [*command, "exec", "-T", "keycloak", "/opt/keycloak/bin/kcadm.sh"]

            def admin(*arguments: str) -> str:
                return subprocess.run(
                    [*base, *arguments],
                    cwd=ROOT,
                    env=env,
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
                env["KEYCLOAK_ADMIN_USERNAME"],
                "--password",
                env["KEYCLOAK_ADMIN_PASSWORD"],
            )
            clients = json.loads(
                admin(
                    "get", "clients", "-r", "a500", "-q", "clientId=a500-device-client"
                )
            )
            client = next(
                item for item in clients if item["clientId"] == "a500-device-client"
            )
            origins = list(dict.fromkeys([*client.get("webOrigins", []), origin]))
            admin(
                "update",
                f"clients/{client['id']}",
                "-r",
                "a500",
                "-s",
                f"webOrigins={json.dumps(origins)}",
            )
            users = json.loads(
                admin("get", "users", "-r", "a500", "-q", "username=admin")
            )
            if not any(user.get("username") == "admin" for user in users):
                admin(
                    "create",
                    "users",
                    "-r",
                    "a500",
                    "-s",
                    "username=admin",
                    "-s",
                    "enabled=true",
                    "-s",
                    "firstName=Admin",
                    "-s",
                    "lastName=Local",
                    "-s",
                    "email=admin@example.com",
                )
                admin(
                    "set-password",
                    "-r",
                    "a500",
                    "--username",
                    "admin",
                    "--new-password",
                    "changeme",
                )
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
            print(f"Ready: {origin} (Keycloak: {keycloak})")
            print("Trusted LAN only: HTTP and default admin credentials are insecure.")
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
        print(f"LAN startup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
