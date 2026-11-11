"""Run isolated Rauthy browser login and protected-API acceptance flow."""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import secrets
import shlex
import ssl
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
AUDIENCE = "a500-api"
CLIENT_ID = "a500-browser-client"
DEVICE_CLIENT_ID = "a500-device-client"
TRADE_CSV_HEADER = (
    "成交日期,成交时间,证券代码,证券名称,操作,成交数量,成交均价,成交金额,"
    "合同编号,成交编号,手续费,印花税,其他杂费,发生金额,资金余额,交易市场,"
    "股东帐户,交收日期,证券中文全称,\n"
)


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
    while time.monotonic() < deadline:
        try:
            with urlopen(issuer.rstrip("/") + "/.well-known/openid-configuration", context=ctx, timeout=3) as response:
                result = json.load(response)
            _validate_discovery(issuer, result)
        except (OSError, TimeoutError, json.JSONDecodeError):
            time.sleep(2)
    raise RuntimeError("Rauthy discovery did not become ready")


def _public_api_request(
    gateway: str,
    path: str,
    token: str | None,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], object]:
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    if content_type is not None:
        request_headers["Content-Type"] = content_type
    request = Request(
        gateway + path,
        data=body,
        headers=request_headers,
        method=method,
    )
    try:
        response = urlopen(request, timeout=15)
    except HTTPError as error:
        response = error
    try:
        payload = response.read()
        try:
            decoded = json.loads(payload) if payload else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AssertionError("Public API returned a non-JSON response") from None
        status = int(getattr(response, "status", response.code))
        return status, {key.lower(): value for key, value in response.headers.items()}, decoded
    finally:
        response.close()


def _multipart_csv(trade_number: int) -> tuple[bytes, str]:
    boundary = "a500-" + secrets.token_hex(12)
    row = (
        "20261030,10:01:02,563360,A500基金,买入,8000,1.264,10112,"
        f"contract-{trade_number},{trade_number},0.510,0.000,0.000,-10112.510,"
        "532232.790,沪Ａ,PRIVATE-ACCOUNT,20261030,A500ETF华泰柏瑞,\n"
    )
    content = (TRADE_CSV_HEADER + row).encode("gb18030")
    multipart = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="fills.csv"\r\n'
        "Content-Type: text/csv\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return multipart, f"multipart/form-data; boundary={boundary}"


def _assert_token_rejected(gateway: str, id_token: str) -> None:
    status, _, _ = _public_api_request(
        gateway, "/api/v1/signals", id_token
    )
    if status != 401:
        raise AssertionError(
            f"Public API accepted an ID token (HTTP {status}, expected 401)"
        )


def _device_flow(
    gateway: str,
    issuer: str,
    discovery: dict[str, object],
) -> tuple[str, str]:
    endpoint = discovery.get("device_authorization_endpoint")
    token_endpoint = discovery.get("token_endpoint")
    if not isinstance(endpoint, str) or not isinstance(token_endpoint, str):
        raise RuntimeError("Rauthy discovery does not advertise the required OIDC endpoints")
    response = urlopen(
        Request(
            endpoint,
            data=urlencode(
                {"client_id": DEVICE_CLIENT_ID, "scope": "openid email"}
            ).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        ),
        timeout=15,
    )
    with response:
        authorization = json.load(response)
    verification_uri = authorization.get("verification_uri_complete") or authorization.get(
        "verification_uri"
    )
    device_code = authorization.get("device_code")
    user_code = authorization.get("user_code")
    if not all(isinstance(value, str) and value for value in (verification_uri, device_code, user_code)):
        raise RuntimeError("Rauthy returned an incomplete device authorization response")
    print(f"Approve the device login at {verification_uri}; user code: {user_code}")
    input("Press Enter after approving the disposable device user: ")
    interval = authorization.get("interval", 5)
    expires_in = authorization.get("expires_in", 300)
    if not isinstance(interval, int) or not isinstance(expires_in, int):
        raise RuntimeError("Rauthy returned invalid device grant timing")
    deadline = time.monotonic() + expires_in
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            response = urlopen(
                Request(
                    token_endpoint,
                    data=urlencode(
                        {
                            "client_id": DEVICE_CLIENT_ID,
                            "device_code": device_code,
                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        }
                    ).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                ),
                timeout=15,
            )
        except HTTPError as error:
            try:
                result = json.loads(error.read())
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise RuntimeError("Rauthy rejected the device grant") from None
            if result.get("error") == "authorization_pending":
                continue
            if result.get("error") == "slow_down":
                interval += 5
                continue
            raise RuntimeError("Rauthy device authorization did not succeed") from None
        with response:
            result = json.load(response)
        access_token, id_token = result.get("access_token"), result.get("id_token")
        if not isinstance(access_token, str) or not isinstance(id_token, str):
            raise AssertionError("Rauthy device grant returned incomplete tokens")
        verify_access_token(access_token, issuer)
        status, _, _ = _public_api_request(gateway, "/api/v1/signals", access_token)
        if status != 200:
            raise AssertionError(f"Device access token returned HTTP {status} from the public API")
        _assert_token_rejected(gateway, id_token)
        return access_token, id_token
    raise RuntimeError("Rauthy device authorization expired before approval")


def _browser_flow(
    gateway: str,
    issuer: str,
    token_endpoint: str,
    *,
    holdings: int,
    cash: str,
) -> tuple[str, str]:
    from playwright.sync_api import sync_playwright

    username = input("Disposable Rauthy browser user email: ").strip()
    password = getpass.getpass("Disposable Rauthy browser user password: ")
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
            if response.url == token_endpoint:
                try:
                    result = response.json()
                except (ValueError, json.JSONDecodeError):
                    return
                candidate = result.get("id_token")
                if isinstance(candidate, str):
                    observed["id_token"] = candidate
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
        if "token" not in observed or "id_token" not in observed:
            raise AssertionError("Browser did not expose both tokens from the real token response")
        verify_access_token(observed["token"], issuer)
        if not signals or signals[-1] != 200:
            raise AssertionError("Protected signal read failed")
        page.goto(gateway + "/assets", wait_until="domcontentloaded")
        page.get_by_role("button", name="创建快照").click()
        page.get_by_label("快照时间").fill(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        page.get_by_label("可用现金", exact=True).fill(cash)
        page.get_by_role("button", name="添加证券").click()
        page.get_by_label("第 1 行证券代码").fill("510300")
        page.get_by_label("第 1 行持有总份额").fill(str(holdings))
        page.get_by_label("第 1 行保护底仓").fill("20")
        page.get_by_label("第 1 行当前可卖").fill(str(holdings - 20))
        page.get_by_label("第 1 行成本基础").fill("3.8500")
        with page.expect_response(lambda r: r.url == gateway + "/api/v1/account-snapshot" and r.request.method == "PUT") as saved:
            page.get_by_role("button", name="创建账户快照", exact=True).click()
        if saved.value.status not in (200, 201):
            raise AssertionError(f"Snapshot write returned HTTP {saved.value.status}")
        page.wait_for_function("() => document.body.innerText.includes('账户快照已保存')")
        readback = page.evaluate("""async token => { const r = await fetch('/api/v1/account-snapshot', {headers: {Authorization: `Bearer ${token}`}}); return {status:r.status, body:await r.json()}; }""", observed["token"])
        if readback["status"] != 200 or readback["body"]["holdings"] != {"510300": holdings} or readback["body"]["available_cash"] != cash:
            raise AssertionError("Snapshot readback did not match browser-written business data")
        access_token, id_token = observed["token"], observed["id_token"]
        context.close()
        browser.close()
        return access_token, id_token


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
        print("Configure browser and device clients plus two disposable users as documented in docs/rauthy-local-e2e.md.")
        input("Press Enter after Admin UI setup: ")
        browser_token, browser_id_token = _browser_flow(
            gateway,
            issuer,
            str(discovery["token_endpoint"]),
            holdings=100,
            cash="125.50",
        )
        _assert_token_rejected(gateway, browser_id_token)
        if _public_api_request(gateway, "/api/v1/account-snapshot", device_token)[0] != 404:
            raise AssertionError("Second Rauthy user could read the browser user's account snapshot")
        if _public_api_request(gateway, "/api/v1/account-snapshot", browser_token)[0] != 200:
            raise AssertionError("Browser user's account snapshot was not available to its owner")
        import_body, import_type = _multipart_csv(22500001)
        status, _, imported = _public_api_request(
            gateway,
            "/api/v1/trade-imports/previews",
            browser_token,
            method="POST",
            body=import_body,
            content_type=import_type,
        )
        if status != 200 or not isinstance(imported, dict):
            raise AssertionError(f"Browser user trade-import preview returned HTTP {status}")
        batch_id = imported.get("batch_id")
        if not isinstance(batch_id, str):
            raise AssertionError("Trade-import preview did not return a batch identifier")
        preview_path = f"/api/v1/trade-imports/previews/{batch_id}"
        if _public_api_request(gateway, preview_path, browser_token)[0] != 200:
            raise AssertionError("Trade-import preview was not visible to its owner")
        if _public_api_request(gateway, preview_path, device_token)[0] != 404:
            raise AssertionError("Second Rauthy user could read the first user's trade-import preview")
        if _public_api_request(
            gateway, preview_path + "/confirm", device_token, method="POST"
        )[0] != 404:
            raise AssertionError("Second Rauthy user could confirm the first user's trade-import preview")
        print("Verified real browser and device access tokens, ID-token rejection, and public API snapshot/trade-import subject isolation.")
    finally:
        subprocess.run([*prefix, "down", "--volumes", "--remove-orphans", "--rmi", "local"], cwd=ROOT, env=env, check=False, timeout=180)


if __name__ == "__main__":
    main()
