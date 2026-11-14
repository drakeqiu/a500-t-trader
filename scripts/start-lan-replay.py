#!/usr/bin/env python3
"""Expose the development stack to LAN clients, optionally using trusted HTTPS."""

import argparse
import ipaddress
import json
import runpy
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
LOCAL = runpy.run_path(str(ROOT / "scripts/start-local.py"))


def validate_hostname(value: str) -> str:
    try:
        hostname = value.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValueError("hostname must be a valid DNS name") from error
    labels = hostname.split(".")
    if (
        len(hostname) > 253
        or len(labels) < 2
        or any(
            not label
            or len(label) > 63
            or not label[0].isalnum()
            or not label[-1].isalnum()
            or any(not (character.isalnum() or character == "-") for character in label)
            for label in labels
        )
    ):
        raise ValueError("hostname must be a valid DNS name with at least two labels")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return hostname
    raise ValueError("hostname must be a DNS name, not an IP address")


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
    parser.add_argument(
        "--hostname",
        help="DNS name clients use; it must resolve to the supplied LAN IPv4 address",
    )
    parser.add_argument(
        "--tls-cert",
        help=(
            "PEM certificate covering --hostname and localhost, "
            "trusted by client browsers"
        ),
    )
    parser.add_argument("--tls-key", help="matching PEM private key")
    parser.add_argument("--timeout", type=int, default=300)
    args, extra = parser.parse_known_args()
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key must be supplied together")
    if extra and args.action != "replay":
        parser.error("only replay accepts additional arguments")
    try:
        host = ipaddress.IPv4Address(args.host)
        if host.is_loopback or host.is_unspecified or host.is_multicast:
            raise ValueError(
                "host must be a LAN IPv4 address, not loopback or a wildcard"
            )
    except ValueError as error:
        parser.error(str(error))
    if args.action == "up" and not args.hostname:
        parser.error(
            "--hostname is required for up; Rauthy requires a DNS name, not an IP RP ID"
        )
    try:
        hostname = validate_hostname(args.hostname) if args.hostname else str(host)
    except ValueError as error:
        parser.error(str(error))
    try:
        env = LOCAL["local_environment"]()
        env.update(MARKET_DATA_MODE="replay")
        LOCAL["check_configuration"](env)
        tls_enabled = args.tls_cert is not None
        if tls_enabled:
            cert = Path(args.tls_cert).expanduser().resolve()
            key = Path(args.tls_key).expanduser().resolve()
            if not cert.is_file() or not key.is_file():
                raise ValueError("--tls-cert and --tls-key must name existing files")
        scheme = "https" if tls_enabled else "http"
        gateway_port = "8443" if tls_enabled else "8080"
        issuer_port = "8444" if tls_enabled else "8081"
        origin = f"{scheme}://{hostname}:{gateway_port}"
        issuer = f"{scheme}://{hostname}:{issuer_port}/auth/v1/"
        env.update(
            GATEWAY_HOST="0.0.0.0",
            GATEWAY_HOST_PORT=gateway_port,
            RAUTHY_HOST="127.0.0.1",
            RAUTHY_HOST_PORT="8081",
            RAUTHY_TLS_HOST="0.0.0.0",
            RAUTHY_TLS_HOST_PORT="8444",
            RAUTHY_PUB_URL=f"{hostname}:{issuer_port}",
            RAUTHY_RP_ID=hostname,
            RAUTHY_RP_ORIGIN=f"{scheme}://{hostname}:{issuer_port}",
            RAUTHY_BROWSER_ORIGIN=origin,
            OIDC_ISSUER=issuer,
            RAUTHY_ADMIN_API_BASE_URL="http://127.0.0.1:8081/auth/v1/",
            OIDC_JWKS_URI="http://rauthy:8080/auth/v1/oidc/certs",
            RABBITMQ_BIND_HOST="127.0.0.1",
        )
        if tls_enabled:
            env.update(
                LAN_TLS="1",
                RAUTHY_LISTEN_SCHEME="http_https",
                TLS_CERT=str(cert),
                TLS_KEY=str(key),
            )
        else:
            env.pop("LAN_TLS", None)
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
                "https://localhost:8444/auth/v1/.well-known/openid-configuration"
                if tls_enabled
                else "http://127.0.0.1:8081/auth/v1/.well-known/openid-configuration"
            )
            while True:
                try:
                    with urlopen(discovery_url, timeout=2) as response:
                        if json.load(response).get("issuer") == env["OIDC_ISSUER"]:
                            break
                except OSError:
                    pass
                except ValueError:
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
                            f"{scheme}://localhost:{gateway_port}{path}", timeout=2
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
            if tls_enabled:
                print("Client browsers must trust the certificate authority.")
            else:
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
