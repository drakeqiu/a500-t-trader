"""Run isolated Rauthy browser login and protected-API acceptance flow."""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
AUDIENCE = "a500-api"
CLIENT_ID = "a500-browser-client"


def _decode(part: str) -> dict[str, object]:
    try:
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AssertionError("Malformed JWT") from exc
    if not isinstance(value, dict):
        raise AssertionError("Malformed JWT")
    return value


def verify_access_token(token: str, issuer: str, *, now: int | None = None) -> None:
    parts = token.split(".")
    if len(parts) != 3:
        raise AssertionError("Expected compact JWT")
    header, claims = _decode(parts[0]), _decode(parts[1])
    current = int(time.time()) if now is None else now
    aud = claims.get("aud")
    iat, exp = claims.get("iat"), claims.get("exp")
    if header.get("alg") != "RS256":
        raise AssertionError("Expected RS256 access token")
    if claims.get("iss") != issuer:
        raise AssertionError("Issuer mismatch")
    if AUDIENCE not in (aud if isinstance(aud, list) else [aud]):
        raise AssertionError("Missing a500-api audience")
    if (not isinstance(iat, int) or isinstance(iat, bool) or not isinstance(exp, int)
            or isinstance(exp, bool) or exp - iat != 300 or exp <= current or iat > current + 30):
        raise AssertionError("Expected five-minute access token")


def internal_jwks_uri(uri: str) -> str:
    parsed = urlsplit(uri)
    if parsed.scheme != "https" or parsed.hostname != "localhost" or not parsed.path:
        raise ValueError("Expected Rauthy HTTPS localhost JWKS URL")
    return urlunsplit(("http", "rauthy:8080", parsed.path, parsed.query, ""))


def _rauthy_settings(port: str) -> dict[str, str]:
    return {
        "OIDC_ISSUER": f"https://localhost:{port}",
        "RAUTHY_HOST_PORT": port,
        "RAUTHY_LISTEN_SCHEME": "https",
        "RAUTHY_PUB_URL": f"localhost:{port}",
    }


def _market_data_images(project: str) -> dict[str, str]:
    return {
        "MARKET_DATA_CONSUMER_IMAGE": f"{project}-market-data-consumer:local",
        "MARKET_DATA_REPLAY_IMAGE": f"{project}-market-data-replay:local",
    }


def _validate_discovery(issuer: str, discovery: dict[str, object]) -> None:
    if discovery.get("issuer") != issuer or not isinstance(discovery.get("jwks_uri"), str):
        raise RuntimeError("Rauthy discovery issuer/JWKS mismatch")


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_discovery(issuer: str) -> dict[str, object]:
    ctx = ssl._create_unverified_context()
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            with urlopen(issuer + "/.well-known/openid-configuration", context=ctx, timeout=3) as response:
                result = json.load(response)
            _validate_discovery(issuer, result)
        except (OSError, TimeoutError, json.JSONDecodeError):
            time.sleep(2)
    raise RuntimeError("Rauthy discovery did not become ready")


def _browser_flow(gateway: str, issuer: str) -> None:
    from playwright.sync_api import sync_playwright

    username = input("Disposable Rauthy user email: ").strip()
    password = getpass.getpass("Disposable Rauthy user password: ")
    if not username or not password:
        raise RuntimeError("Disposable user credentials are required")
    observed: dict[str, str] = {}
    signals: list[int] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        def request_seen(request) -> None:
            if request.url.startswith(gateway + "/api/"):
                auth = request.headers.get("authorization", "")
                if auth.startswith("Bearer "):
                    observed["token"] = auth[7:]

        def response_seen(response) -> None:
            if urlsplit(response.url).path == "/api/v1/signals" and response.request.method == "GET":
                signals.append(response.status)

        page.on("request", request_seen)
        page.on("response", response_seen)
        page.goto(gateway + "/signals", wait_until="domcontentloaded")
        page.get_by_role("button", name="安全连接", exact=True).first.click()
        page.locator('input[type="email"]').fill(username)
        page.locator('input[type="password"]').fill(password)
        page.locator('button[type="submit"]').click()
        page.wait_for_url(gateway + "/**", timeout=120_000)
        page.goto(gateway + "/signals", wait_until="domcontentloaded")
        page.wait_for_function("() => document.body.innerText.includes('策略信号')", timeout=60_000)
        page.wait_for_timeout(500)
        if "token" not in observed:
            raise AssertionError("Browser did not send authenticated gateway API request")
        verify_access_token(observed["token"], issuer)
        if not signals or signals[-1] != 200:
            raise AssertionError("Protected signal read failed")
        page.goto(gateway + "/assets", wait_until="domcontentloaded")
        page.get_by_role("button", name="创建快照").click()
        page.get_by_label("快照时间").fill(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        page.get_by_label("可用现金", exact=True).fill("125.50")
        page.get_by_role("button", name="添加证券").click()
        page.get_by_label("第 1 行证券代码").fill("510300")
        page.get_by_label("第 1 行持有总份额").fill("100")
        page.get_by_label("第 1 行保护底仓").fill("20")
        page.get_by_label("第 1 行当前可卖").fill("80")
        page.get_by_label("第 1 行成本基础").fill("3.8500")
        with page.expect_response(lambda r: r.url == gateway + "/api/v1/account-snapshot" and r.request.method == "PUT") as saved:
            page.get_by_role("button", name="创建账户快照", exact=True).click()
        if saved.value.status not in (200, 201):
            raise AssertionError(f"Snapshot write returned HTTP {saved.value.status}")
        page.wait_for_function("() => document.body.innerText.includes('账户快照已保存')")
        readback = page.evaluate("""async token => { const r = await fetch('/api/v1/account-snapshot', {headers: {Authorization: `Bearer ${token}`}}); return {status:r.status, body:await r.json()}; }""", observed["token"])
        if readback["status"] != 200 or readback["body"]["holdings"] != {"510300": 100} or readback["body"]["available_cash"] != "125.50":
            raise AssertionError("Snapshot readback did not match browser-written business data")
        print("Verified PKCE browser login, token claims, protected signal read, snapshot write/read.")
        context.close()
        browser.close()


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    command = shlex.split(os.environ.get("PODMAN_COMPOSE", "podman compose"))
    if not command or shutil.which(command[0]) is None or "podman" not in Path(command[0]).name:
        raise RuntimeError("Podman Compose is required")
    project = "a500-rauthy-" + secrets.token_hex(6)
    ports = {name: str(_port()) for name in ("rauthy", "gateway", "postgres", "rabbit")}
    issuer = f"https://localhost:{ports['rauthy']}"
    gateway = f"http://localhost:{ports['gateway']}"
    env = dict(os.environ)
    for name in ("KEYCLOAK_ADMIN_USERNAME", "KEYCLOAK_ADMIN_PASSWORD", "AD_USERNAME", "AD_PASSWORD", "KEYCLOAK_HOSTNAME", "KEYCLOAK_HOST_PORT", "TLS_CERT", "TLS_KEY"):
        env.pop(name, None)
    env.update({"POSTGRES_PASSWORD": secrets.token_urlsafe(32), "POSTGRES_PORT": ports["postgres"],
                "RABBITMQ_BIND_HOST": "127.0.0.1", "RABBITMQ_HOST_PORT": ports["rabbit"],
                "OIDC_AUDIENCE": AUDIENCE, "OIDC_JWKS_URI": "http://rauthy:8080/auth/v1/oidc/jwks",
                "OIDC_CLIENT_ID": CLIENT_ID, "GATEWAY_HOST": "127.0.0.1",
                "GATEWAY_HOST_PORT": ports["gateway"], "MARKET_DATA_MODE": "replay",
                **_rauthy_settings(ports["rauthy"]), **_market_data_images(project)})
    files = ["compose.yaml", "compose.web.yaml", "compose.web-http.yaml", "compose.rauthy.yaml"]
    prefix = [*command, "--env-file", os.devnull, "--project-name", project]
    for file in files:
        prefix.extend(("-f", str(ROOT / file)))
    try:
        subprocess.run([*prefix, "build"], cwd=ROOT, env=env, check=True, timeout=900)
        subprocess.run([*prefix, "up", "-d", "rauthy"], cwd=ROOT, env=env, check=True, timeout=180)
        discovery = _wait_discovery(issuer)
        env["OIDC_JWKS_URI"] = internal_jwks_uri(str(discovery["jwks_uri"]))
        subprocess.run([*prefix, "up", "-d", "signal-api", "account-api", "jobs-api", "frontend", "gateway"], cwd=ROOT, env=env, check=True, timeout=300)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                with urlopen(gateway + "/", timeout=3):
                    break
            except OSError:
                time.sleep(2)
        else:
            raise RuntimeError("Public gateway did not become ready")
        print(f"Rauthy: {issuer}; public gateway: {gateway}; Admin UI: {issuer}/auth/v1/admin/")
        print("Configure documented public client and disposable user in docs/rauthy-local-e2e.md.")
        input("Press Enter after Admin UI setup: ")
        _browser_flow(gateway, issuer)
    finally:
        subprocess.run([*prefix, "down", "--volumes", "--remove-orphans", "--rmi", "local"], cwd=ROOT, env=env, check=False, timeout=180)


if __name__ == "__main__":
    main()
