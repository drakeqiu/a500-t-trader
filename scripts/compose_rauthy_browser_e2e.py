"""Run isolated Rauthy browser login and protected-API acceptance flow."""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener, urlopen

def _set_client_audience(issuer: str, key_name: str, key_secret: str) -> None:
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl._create_unverified_context()),
    )
    headers = {"Authorization": f"API-Key {key_name}${key_secret}"}
    fields = (
        "name", "confidential", "redirect_uris", "post_logout_redirect_uris",
        "allowed_origins", "enabled", "flows_enabled", "access_token_alg",
        "id_token_alg", "auth_code_lifetime", "access_token_lifetime",
        "scopes", "default_scopes", "challenges", "force_mfa", "client_uri",
        "contacts", "backchannel_logout_uri", "restrict_group_prefix",
        "claims", "claims_at_root", "allowed_resources", "scim",
    )
    for client_id in (CLIENT_ID, DEVICE_CLIENT_ID):
        endpoint = issuer.rstrip("/") + f"/clients/{client_id}"
        with opener.open(Request(endpoint, headers=headers), timeout=10) as response:
            client = json.load(response)
        update = {field: client.get(field) for field in fields}
        update["default_aud"] = [AUDIENCE]
        request = Request(
            endpoint,
            data=json.dumps(update).encode(),
            headers={**headers, "Content-Type": "application/json"},
            method="PUT",
        )
        with opener.open(request, timeout=10) as response:
            if response.status not in (200, 204):
                raise RuntimeError(f"Rauthy client audience update returned HTTP {response.status}")
        with opener.open(Request(endpoint, headers=headers), timeout=10) as response:
            configured = json.load(response)
        if configured.get("default_aud") != [AUDIENCE]:
            raise AssertionError("Rauthy did not persist the client's default_aud")


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
        "RAUTHY_LISTEN_SCHEME": "http_https",
        "RAUTHY_PUB_URL": f"localhost:{port}",
    }




def _validate_discovery(issuer: str, discovery: dict[str, object]) -> None:
    if discovery.get("issuer") != issuer or not isinstance(discovery.get("jwks_uri"), str):
        raise RuntimeError("Rauthy discovery issuer/JWKS mismatch")


def _write_bootstrap(bootstrap_dir: Path, gateway: str) -> tuple[str, str, str]:
    bootstrap_dir.mkdir(mode=0o711, parents=True, exist_ok=True)
    bootstrap_dir.chmod(0o711)
    password = secrets.token_urlsafe(24)
    users = [
        {
            "email": email,
            "given_name": email.split("@", 1)[0],
            "family_name": "Acceptance",
            "password": {"Plain": password},
            "enabled": True,
            "email_verified": True,
            "roles": roles,
        }
        for email, roles in (
            ("browser@example.test", []),
            ("device@example.test", []),
            ("identity-admin@example.test", ["admin"]),
        )
    ]
    clients = [
        {
            "id": client_id,
            "name": client_id,
            "secret": None,
            "enabled": True,
            "redirect_uris": [gateway + "/"] if redirect else [],
            "allowed_origins": [gateway] if redirect else [],
            "flows_enabled": flows,
            "access_token_alg": "RS256",
            "id_token_alg": "RS256",
            "auth_code_lifetime": 60,
            "access_token_lifetime": 300,
            "scopes": ["openid", "profile", "email"],
            "default_scopes": ["openid", "profile", "email"],
            "force_mfa": False,
        }
        for client_id, redirect, flows in (
            (CLIENT_ID, True, ["authorization_code", "refresh_token"]),
            (DEVICE_CLIENT_ID, False, ["urn:ietf:params:oauth:grant-type:device_code"]),
        )
    ]
    key_name = "a500-acceptance"
    api_secret = secrets.token_hex(32)
    api_keys = [{
        "name": key_name,
        "secret": {"Plain": api_secret},
        "exp": int(time.time()) + 1800,
        "access": [
            {"group": "Clients", "access_rights": ["read", "create", "update"]},
            {"group": "Users", "access_rights": ["read", "update"]},
        ],
    }]
    for filename, value in (
        ("users.json", users),
        ("clients.json", clients),
        ("api_keys.json", api_keys),
    ):
        path = bootstrap_dir / filename
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o644)
    return key_name, api_secret, password


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
            return result
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
    username: str,
    password: str,
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
        context=ssl._create_unverified_context(),
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
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.goto(verification_uri, wait_until="domcontentloaded")
        page.locator('input[type="email"]').fill(username)
        page.locator('button[type="submit"]').click()
        password_input = page.locator('input[type="password"]:visible')
        password_input.wait_for(state="visible", timeout=30_000)
        password_input.fill(password)
        page.locator('button[type="submit"]').click()
        page.locator('input[name="userCode"]').wait_for(timeout=60_000)
        code_input = page.locator('input[name="userCode"]')
        if not code_input.input_value():
            code_input.fill(user_code)
        page.locator("button").first.click()
        page.locator("ul li").first.wait_for(timeout=30_000)
        page.locator("button").first.click()
        page.wait_for_timeout(2500)
        context.close()
        browser.close()
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
                context=ssl._create_unverified_context(),
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
    username: str,
    password: str,
    *,
    holdings: int = 100,
    cash: str = "125.50",
    create_snapshot: bool = True,
) -> tuple[str, str | None]:
    from playwright.sync_api import sync_playwright

    if not username or not password:
        raise RuntimeError("Disposable user credentials are required")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.goto(gateway + "/signals", wait_until="domcontentloaded")
        page.get_by_role("button", name="安全连接", exact=True).first.click()
        page.wait_for_url(issuer + "**", timeout=120_000)
        username_field = page.locator(
            'input[autocomplete="username"], input[type="email"], input[name="login"], input[type="text"]'
        ).first
        print(
            "Rauthy login form:",
            {
                "url": page.url,
                "title": page.title(),
                "inputs": page.locator("input").evaluate_all(
                    "(nodes) => nodes.map((node) => ({type: node.type, name: node.name, autocomplete: node.autocomplete}))"
                ),
                "body": page.locator("body").inner_text()[:1500],
            },
        )
        username_field.wait_for(state="visible", timeout=30_000)
        username_field.fill(username)
        page.locator("form").get_by_role("button", name="Login", exact=True).click()
        password_field = page.locator('input[type="password"]:visible')
        password_field.wait_for(state="visible", timeout=30_000)
        password_field.fill(password)
        with page.expect_response(
            lambda response: "/oidc/token" in urlsplit(response.url).path
            and response.request.method == "POST",
            timeout=60_000,
        ) as token_response:
            page.locator('button[type="submit"]:visible').click()
        page.wait_for_url(gateway + "/**", timeout=120_000)
        token_body = token_response.value.json()
        browser_token = token_body.get("access_token")
        refresh_token = token_body.get("refresh_token")
        if not isinstance(browser_token, str):
            raise AssertionError("Browser OAuth response did not contain an access token")
        if refresh_token is not None and not isinstance(refresh_token, str):
            raise AssertionError("Browser OAuth response contained an invalid refresh token")
        page.wait_for_function("() => document.body.innerText.includes('策略信号')", timeout=60_000)
        verify_access_token(browser_token, issuer)
        status, _, api_error = _public_api_request(
            gateway, "/api/v1/signals", browser_token
        )
        if status != 200:
            raise AssertionError(
                f"Browser access token was rejected by public API (HTTP {status}: {api_error})"
            )

        if create_snapshot:
            page.get_by_role("link", name="资产管理", exact=True).click()
            page.get_by_role("button", name="创建快照").wait_for(timeout=30_000)

        if create_snapshot:
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
            readback = page.evaluate("""async token => { const r = await fetch('/api/v1/account-snapshot', {headers: {Authorization: `Bearer ${token}`}}); return {status:r.status, body:await r.json()}; }""", browser_token)
            if readback["status"] != 200 or readback["body"]["holdings"] != {"510300": holdings} or readback["body"]["available_cash"] != cash:
                raise AssertionError("Snapshot readback did not match browser-written business data")
        context.close()
        browser.close()
        return browser_token, refresh_token


def _disable_user(issuer: str, key_name: str, key_secret: str, email: str) -> None:
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl._create_unverified_context()),
    )
    headers = {"Authorization": f"API-Key {key_name}${key_secret}"}
    endpoint = issuer.rstrip("/") + f"/users/{email}"
    with opener.open(Request(endpoint, headers=headers), timeout=10) as response:
        user = json.load(response)
    if not isinstance(user, dict) or user.get("email") != email:
        raise AssertionError("Rauthy did not return the expected disposable user")
    user["enabled"] = False
    request = Request(
        endpoint,
        data=json.dumps(user).encode(),
        headers={**headers, "Content-Type": "application/json"},
        method="PUT",
    )
    with opener.open(request, timeout=10) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f"Rauthy user disable returned HTTP {response.status}")
    with opener.open(Request(endpoint, headers=headers), timeout=10) as response:
        if json.load(response).get("enabled") is not False:
            raise AssertionError("Rauthy did not persist the disabled user state")


def _refresh_token(token_endpoint: str, client_id: str, refresh_token: str) -> int:
    request = Request(
        token_endpoint,
        data=urlencode({
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        }).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            return response.status
    except HTTPError as error:
        error.close()
        return error.code


def _new_login_rejected(
    gateway: str, issuer: str, username: str, password: str
) -> bool:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(ignore_https_errors=True)
        try:
            page.goto(gateway + "/signals", wait_until="domcontentloaded")
            page.get_by_role("button", name="安全连接", exact=True).first.click()
            page.wait_for_url(issuer + "**", timeout=30_000)
            page.locator(
                'input[autocomplete="username"], input[type="email"], input[name="login"], input[type="text"]'
            ).first.fill(username)
            page.locator("form").get_by_role("button", name="Login", exact=True).click()
            page.locator('input[type="password"]:visible').wait_for(timeout=30_000)
            page.locator('input[type="password"]:visible').fill(password)
            page.locator('button[type="submit"]:visible').click()
            try:
                page.wait_for_url(gateway + "/**", timeout=20_000)
                return False
            except Exception:
                # A rejected credential must leave the user on the IdP login page.
                return page.url.startswith(issuer) and page.locator(
                    'input[type="password"]:visible'
                ).count() > 0
        finally:
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
    env.setdefault("UV_INDEX_URL", "https://mirrors.aliyun.com/pypi/simple/")
    for name in ("KEYCLOAK_ADMIN_USERNAME", "KEYCLOAK_ADMIN_PASSWORD", "AD_USERNAME", "AD_PASSWORD", "KEYCLOAK_HOSTNAME", "KEYCLOAK_HOST_PORT", "TLS_CERT", "TLS_KEY"):
        env.pop(name, None)
    temp = tempfile.TemporaryDirectory(prefix=project + "-")
    Path(temp.name).chmod(0o711)
    bootstrap_dir = Path(temp.name) / "bootstrap"
    api_key_name, api_key_secret, user_password = _write_bootstrap(bootstrap_dir, gateway)
    encryption_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    subprocess.run(["uv", "build", "--wheel"], cwd=ROOT, env=env, check=True, timeout=120)
    env.update({
        "ACCOUNT_API_MARKET_DATA": "disabled",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "POSTGRES_PORT": ports["postgres"],
        "RABBITMQ_BIND_HOST": "127.0.0.1",
        "RABBITMQ_HOST_PORT": ports["rabbit"],
        "OIDC_ISSUER": issuer,
        "OIDC_AUDIENCE": AUDIENCE,
        "OIDC_JWKS_URI": "http://rauthy:8080/auth/v1/oidc/jwks",
        "OIDC_CLIENT_ID": CLIENT_ID,
        "GATEWAY_HOST": "127.0.0.1",
        "GATEWAY_HOST_PORT": ports["gateway"],
        "MARKET_DATA_MODE": "replay",
        "RAUTHY_LOCAL_TEST": "false",
        "RAUTHY_BOOTSTRAP_HOST_DIR": str(bootstrap_dir),
        "RAUTHY_NODE_ID": "1",
        "RAUTHY_NODES": "1 localhost:8100 localhost:8200",
        "RAUTHY_SECRET_RAFT": secrets.token_urlsafe(32),
        "RAUTHY_SECRET_API": secrets.token_urlsafe(32),
        "RAUTHY_ENC_KEYS": "test/" + encryption_key,
        "RAUTHY_ENC_KEY_ACTIVE": "test",
        **_rauthy_settings(ports["rauthy"]),
    })
    files = ["compose.yaml", "compose.web.yaml", "compose.web-http.yaml", "compose.rauthy.yaml"]
    prefix = [*command, "--env-file", os.devnull, "--project-name", project]
    for file in files:
        prefix.extend(("-f", str(ROOT / file)))
    try:
        subprocess.run(
            [*prefix, "build", "account-api", "frontend"],
            cwd=ROOT,
            env=env,
            check=True,
            timeout=900,
        )
        api_image = f"localhost/{project}_account-api:latest"
        for service in ("signal-api", "jobs-api"):
            subprocess.run(
                [
                    "podman",
                    "image",
                    "tag",
                    api_image,
                    f"localhost/{project}_{service}:latest",
                ],
                cwd=ROOT,
                env=env,
                check=True,
                timeout=30,
            )
        subprocess.run([*prefix, "up", "-d", "rauthy"], cwd=ROOT, env=env, check=True, timeout=180)
        discovery = _wait_discovery(issuer)
        _set_client_audience(issuer, api_key_name, api_key_secret)
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
        browser_token, browser_refresh_token = _browser_flow(
            gateway,
            issuer,
            str(discovery["token_endpoint"]),
            "browser@example.test",
            user_password,
        )
        device_token, device_id_token = _device_flow(
            gateway, issuer, discovery, "device@example.test", user_password
        )
        admin_token, _ = _browser_flow(
            gateway,
            issuer,
            str(discovery["token_endpoint"]),
            "identity-admin@example.test",
            user_password,
            create_snapshot=False,
        )
        _assert_token_rejected(gateway, device_id_token)
        for token in (None, "not.a.jwt"):
            if _public_api_request(gateway, "/api/v1/signals", token)[0] != 401:
                raise AssertionError("Public API accepted an absent or malformed bearer token")
        if _public_api_request(gateway, "/api/v1/account-snapshot", device_token)[0] != 404:
            raise AssertionError("Second Rauthy user could read the browser user's account snapshot")
        if _public_api_request(gateway, "/api/v1/account-snapshot", admin_token)[0] != 404:
            raise AssertionError("Rauthy identity admin could read the browser user's account snapshot")
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
        # Renew the disposable session immediately before disabling the account.
        browser_token, browser_refresh_token = _browser_flow(
            gateway,
            issuer,
            str(discovery["token_endpoint"]),
            "browser@example.test",
            user_password,
            create_snapshot=False,
        )
        if not browser_refresh_token:
            raise AssertionError("Rauthy did not issue the browser refresh credential")
        token_exp = _decode(browser_token.split(".")[1]).get("exp")
        if not isinstance(token_exp, int):
            raise AssertionError("Browser access token has no expiry")
        _disable_user(issuer, api_key_name, api_key_secret, "browser@example.test")
        if _public_api_request(gateway, "/api/v1/signals", browser_token)[0] != 200:
            raise AssertionError("Previously issued token did not remain valid after user disable")
        if _refresh_token(str(discovery["token_endpoint"]), CLIENT_ID, browser_refresh_token) not in (400, 401):
            raise AssertionError("Rauthy accepted refresh after the user was disabled")
        if not _new_login_rejected(
            gateway, issuer, "browser@example.test", user_password,
        ):
            raise AssertionError("Rauthy accepted a new browser login after the user was disabled")
        wait_seconds = max(0, token_exp - int(time.time())) + 1
        print(
            "Disabled disposable browser user: prior token remains locally valid; "
            f"waiting {wait_seconds}s for its five-minute expiry."
        )
        time.sleep(wait_seconds)
        expiry_status = _public_api_request(gateway, "/api/v1/signals", browser_token)[0]
        if expiry_status != 401:
            raise AssertionError(
                f"Previously issued access token returned HTTP {expiry_status} after expiry"
            )
        print(
            "Verified disabled-user login and refresh rejection; prior access token "
            "was accepted during its residual window and rejected after expiry."
        )
        print("Verified real browser and device access tokens, ID-token rejection, and public API snapshot/trade-import subject isolation.")
    except BaseException:
        subprocess.run(
            [*prefix, "logs", "account-api", "signal-api", "gateway"],
            cwd=ROOT,
            env=env,
            check=False,
            timeout=30,
        )
        raise
    finally:
        subprocess.run([*prefix, "down", "--volumes", "--remove-orphans", "--rmi", "local"], cwd=ROOT, env=env, check=False, timeout=180)
        temp.cleanup()


if __name__ == "__main__":
    main()
