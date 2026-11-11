"""Run isolated Rauthy browser login and protected-API acceptance flow."""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import secrets
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPSHandler, HTTPError, ProxyHandler, Request, build_opener, urlopen
ROOT = Path(__file__).resolve().parents[1]
AUDIENCE = "a500-api"
CLIENT_ID = "a500-browser-client"
DEVICE_CLIENT_ID = "a500-device-client"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"






def protocol_error(payload: dict[str, object]) -> str | None:
    error = payload.get("error")
    return error if isinstance(error, str) else None


def _oauth_post(opener, endpoint: str, form: dict[str, str]) -> tuple[int, dict[str, object]]:
    request = Request(endpoint, data=urlencode(form).encode(),
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, json.load(response)
    except HTTPError as response:
        try:
            return response.code, json.load(response)
        finally:
            response.close()


def _device_grant(gateway: str, issuer: str, username: str, password: str) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl._create_unverified_context()))
    endpoint = issuer + "oidc/"

    def start() -> dict[str, object]:
        status, response = _oauth_post(opener, endpoint + "device", {
            "client_id": DEVICE_CLIENT_ID, "scope": "openid",
        })
        if status != 200:
            raise AssertionError(f"Device authorization returned HTTP {status}")
        return response

    def poll(code: str) -> tuple[int, dict[str, object]]:
        return _oauth_post(opener, endpoint + "token", {
            "grant_type": DEVICE_GRANT, "device_code": code, "client_id": DEVICE_CLIENT_ID,
        })

    def decision(payload: dict[str, object], accept: bool) -> None:
        complete_uri = payload.get("verification_uri_complete")
        if not isinstance(complete_uri, str):
            raise AssertionError("Device authorization omitted verification URI")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(ignore_https_errors=True)
            page.goto(complete_uri, wait_until="domcontentloaded")
            if page.locator('input[type="email"]').count():
                page.locator('input[type="email"]').fill(username)
                page.locator('input[type="password"]').fill(password)
                page.locator('button[type="submit"]').click()
                page.wait_for_load_state("domcontentloaded")
            names = r"accept|approve|allow|confirm|authorize|批准|允许" if accept else r"decline|deny|拒绝"
            try:
                page.get_by_role("button", name=re.compile(names, re.I)).click(timeout=30_000)
            except PlaywrightTimeoutError as exc:
                raise AssertionError("Rauthy verification decision was not available") from exc
            page.wait_for_timeout(300)
            browser.close()

    approved = start()
    code, lifetime, interval = (approved.get("device_code"), approved.get("expires_in"), approved.get("interval"))
    if not isinstance(code, str) or not isinstance(lifetime, int) or not isinstance(interval, int):
        raise AssertionError("Malformed device authorization response")
    time.sleep(interval + 1)
    status, response = poll(code)
    if status != 400 or protocol_error(response) != "authorization_pending":
        raise AssertionError("Expected authorization_pending before browser approval")
    print("Device protocol: authorization_pending.")
    status, response = poll(code)
    if status != 400 or protocol_error(response) != "slow_down":
        raise AssertionError("Expected slow_down after an early poll")
    print("Device protocol: slow_down; respecting updated poll interval.")
    time.sleep(interval + 5)
    decision(approved, True)
    status, response = poll(code)
    if status != 200 or not isinstance(response.get("access_token"), str):
        raise AssertionError(f"Device approval failed: {protocol_error(response)}")
    token = str(response["access_token"])
    verify_access_token(token, issuer)
    _device_public_api(gateway, token)
    print("Device protocol: approved; RS256, default audience and five-minute token verified.")

    denied = start()
    denied_code = denied.get("device_code")
    if not isinstance(denied_code, str):
        raise AssertionError("Malformed denial device authorization response")
    decision(denied, False)
    status, response = poll(denied_code)
    if status != 400 or protocol_error(response) != "expired_token":
        raise AssertionError("Denied device authorization did not terminate")
    print("Device protocol: user denial terminated without a token.")

    expired = start()
    expired_code, expired_lifetime = expired.get("device_code"), expired.get("expires_in")
    if not isinstance(expired_code, str) or not isinstance(expired_lifetime, int):
        raise AssertionError("Malformed expiry device authorization response")
    print(f"Device protocol: waiting for natural expiry ({expired_lifetime}s).")
    time.sleep(expired_lifetime + 1)
    status, response = poll(expired_code)
    if status != 400 or protocol_error(response) != "expired_token":
        raise AssertionError("Expired device authorization did not terminate")
    print("Device protocol: expiry terminated without a token.")


def _device_public_api(gateway: str, token: str) -> None:
    opener = build_opener(ProxyHandler({}))
    headers = {"Authorization": f"Bearer {token}"}
    with opener.open(Request(gateway + "/api/v1/signals", headers=headers), timeout=20) as response:
        if response.status != 200:
            raise AssertionError(f"Public signal API returned HTTP {response.status}")
    snapshot = {
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "holdings": {"510300": 100}, "protected_floor": {"510300": 20},
        "sellable": {"510300": 80}, "cost_basis": {"510300": "3.8500"},
        "available_cash": "125.50",
    }
    request = Request(gateway + "/api/v1/account-snapshot", data=json.dumps(snapshot).encode(),
                      headers={**headers, "Content-Type": "application/json", "If-None-Match": "*"},
                      method="PUT")
    with opener.open(request, timeout=20) as response:
        if response.status not in (200, 201):
            raise AssertionError(f"Public snapshot write returned HTTP {response.status}")
    with opener.open(Request(gateway + "/api/v1/account-snapshot", headers=headers), timeout=20) as response:
        saved = json.load(response)
    if saved.get("holdings") != {"510300": 100} or saved.get("available_cash") != "125.50":
        raise AssertionError("Public snapshot readback mismatch")
    request = Request(gateway + "/api/v1/signals/stream?symbols=510300", headers=headers)
    with opener.open(request, timeout=20) as response:
        if response.status != 200 or response.headers.get_content_type() != "text/event-stream":
            raise AssertionError("Public authenticated SSE stream failed")
        if not response.readline():
            raise AssertionError("Public SSE stream returned no initial event")


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
        "OIDC_ISSUER": f"https://localhost:{port}/auth/v1/",
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
    failures: dict[str, int] = {}
    last_status: int | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(issuer.rstrip("/") + "/.well-known/openid-configuration", context=ctx, timeout=3) as response:
                result = json.load(response)
            _validate_discovery(issuer, result)
            return result
        except HTTPError as error:
            last_status = error.code
            failures["http_error"] = failures.get("http_error", 0) + 1
            error.close()
        except (OSError, TimeoutError, json.JSONDecodeError) as error:
            category = type(error).__name__
            failures[category] = failures.get(category, 0) + 1
        time.sleep(2)
    counts = ", ".join(f"{kind}={count}" for kind, count in sorted(failures.items())) or "none"
    status = f", last_http_status={last_status}" if last_status is not None else ""
    raise RuntimeError(f"Rauthy discovery did not become ready ({counts}{status})")


def _browser_flow(gateway: str, issuer: str, username: str, password: str) -> None:
    from playwright.sync_api import sync_playwright
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
    issuer = f"https://localhost:{ports['rauthy']}/auth/v1/"
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
        print(f"Rauthy: {issuer}; public gateway: {gateway}; Admin UI: {issuer}admin/")
        print("在临时实例中按 docs/rauthy-local-e2e.md 配置一次性用户及两个独立公开客户端；")
        print("为两个客户端配置 default_aud=a500-api，并核实设备客户端启用 device_code grant。")
        username = input("Disposable Rauthy user email: ").strip()
        password = getpass.getpass("Disposable Rauthy user password: ")
        if not username or not password:
            raise RuntimeError("Disposable user credentials are required")
        _browser_flow(gateway, issuer, username, password)
        _device_grant(gateway, issuer, username, password)
    finally:
        subprocess.run([*prefix, "down", "--volumes", "--remove-orphans", "--rmi", "local"], cwd=ROOT, env=env, check=False, timeout=180)


if __name__ == "__main__":
    main()
