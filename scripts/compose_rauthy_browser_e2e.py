"""Run isolated Rauthy browser login and protected-API acceptance flow."""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener, urlopen


def _recovery_link(messages: list[dict[str, object]], hostname: str) -> str:
    for message in messages:
        text = html.unescape(
            "\n".join(
                str(message.get(field, "")) for field in ("Text", "HTML", "Subject")
            )
        )
        for match in re.finditer(r"https?://[^\s\"'<>]+", text):
            link = match.group(0).rstrip(").,;")
            parsed = urlsplit(link)
            if parsed.hostname != hostname or "reset" not in parsed.path.lower():
                continue
            return link
    raise AssertionError("Mailpit message has no recovery link for local Rauthy")


def _mailpit_recovery_link(mailpit: str, email: str, hostname: str) -> str:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urlopen(mailpit + "/api/v1/messages", timeout=5) as response:
                result = json.load(response)
            messages = result.get("messages", []) if isinstance(result, dict) else []
            for message in messages:
                if (
                    not isinstance(message, dict)
                    or email.lower() not in json.dumps(message).lower()
                ):
                    continue
                message_id = message.get("ID")
                if not isinstance(message_id, str):
                    continue
                with urlopen(
                    mailpit + "/api/v1/message/" + message_id, timeout=5
                ) as response:
                    detail = json.load(response)
                if isinstance(detail, dict):
                    return _recovery_link([detail], hostname)
        except OSError, TimeoutError, json.JSONDecodeError:
            pass
        time.sleep(1)
    raise RuntimeError("Timed out waiting for local password recovery email")


def _email_update_payload(user: dict[str, object], new_email: str) -> dict[str, object]:
    updated = dict(user)
    updated["email"] = new_email
    return updated


def _perform_password_recovery(
    issuer: str, mailpit: str, email: str, new_password: str
) -> None:
    from playwright.sync_api import sync_playwright

    hostname = urlsplit(issuer).hostname or "localhost"
    stage = "opening the password-reset request page"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(ignore_https_errors=True)
        try:
            page.goto(issuer + "users/password_reset", wait_until="domcontentloaded")
            stage = "submitting the reset request"
            page.get_by_role("textbox").first.fill(email)
            page.locator('button[type="submit"]:visible').click()
            page.locator(".success").wait_for(state="visible", timeout=30_000)
            stage = "retrieving the reset email"
            link = _mailpit_recovery_link(mailpit, email, hostname)
            page.goto(link, wait_until="domcontentloaded")
            stage = "submitting the new password"
            password_fields = page.locator('input[type="password"]:visible')
            password_fields.first.fill(new_password)
            if password_fields.count() > 1:
                password_fields.nth(1).fill(new_password)
            page.locator('button[type="submit"]:visible').click()
            page.wait_for_load_state("domcontentloaded")
        except Exception as exc:
            raise RuntimeError(
                f"Local password recovery failed while {stage} ({type(exc).__name__})"
            ) from None
        finally:
            browser.close()


def _change_user_email(
    issuer: str, key_name: str, key_secret: str, old_email: str, new_email: str
) -> str:
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl._create_unverified_context()),
    )
    headers = {"Authorization": f"API-Key {key_name}${key_secret}"}
    endpoint = issuer.rstrip("/") + f"/users/{old_email}"
    with opener.open(Request(endpoint, headers=headers), timeout=10) as response:
        user = json.load(response)
    if not isinstance(user, dict) or user.get("email") != old_email:
        raise AssertionError("Rauthy did not return the expected disposable user")
    user_id = user.get("id")
    if not isinstance(user_id, str) or not user_id:
        raise AssertionError("Rauthy user response did not contain a stable subject id")
    request = Request(
        endpoint,
        data=json.dumps(_email_update_payload(user, new_email)).encode(),
        headers={**headers, "Content-Type": "application/json"},
        method="PUT",
    )
    with opener.open(request, timeout=10) as response:
        if response.status not in (200, 204):
            raise RuntimeError(
                f"Rauthy user email update returned HTTP {response.status}"
            )
    with opener.open(
        Request(issuer.rstrip("/") + f"/users/{new_email}", headers=headers),
        timeout=10,
    ) as response:
        updated_user = json.load(response)
    if updated_user.get("email") != new_email or updated_user.get("id") != user_id:
        raise AssertionError("Rauthy email update changed or lost the user's subject")
    return user_id


def _set_client_audience(issuer: str, key_name: str, key_secret: str) -> None:
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl._create_unverified_context()),
    )
    headers = {"Authorization": f"API-Key {key_name}${key_secret}"}
    fields = (
        "name",
        "confidential",
        "redirect_uris",
        "post_logout_redirect_uris",
        "allowed_origins",
        "enabled",
        "flows_enabled",
        "access_token_alg",
        "id_token_alg",
        "auth_code_lifetime",
        "access_token_lifetime",
        "scopes",
        "default_scopes",
        "challenges",
        "force_mfa",
        "client_uri",
        "contacts",
        "backchannel_logout_uri",
        "restrict_group_prefix",
        "claims",
        "claims_at_root",
        "allowed_resources",
        "scim",
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
                raise RuntimeError(
                    f"Rauthy client audience update returned HTTP {response.status}"
                )
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
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def protocol_error(payload: dict[str, object]) -> str | None:
    error = payload.get("error")
    return error if isinstance(error, str) else None


def _oauth_post(
    opener, endpoint: str, form: dict[str, str]
) -> tuple[int, dict[str, object]]:
    request = Request(
        endpoint,
        data=urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, json.load(response)
    except HTTPError as response:
        try:
            return response.code, json.load(response)
        finally:
            response.close()


def _submit_password_step(page, password: str) -> None:
    page.locator('button[type="submit"]').click()
    password_input = page.locator('input[type="password"]:visible')
    password_input.wait_for(state="visible", timeout=30_000)
    password_input.fill(password)
    page.locator('button[type="submit"]').click()


def _run_device_browser_step(step: Callable[[], None]) -> None:
    try:
        step()
    except Exception:
        raise AssertionError(
            "Rauthy device authorization browser interaction failed"
        ) from None


def _device_grant(
    gateway: str, issuer: str, username: str, password: str
) -> tuple[str, str]:
    from playwright.sync_api import sync_playwright

    opener = build_opener(
        ProxyHandler({}), HTTPSHandler(context=ssl._create_unverified_context())
    )
    endpoint = issuer + "oidc/"

    def start() -> dict[str, object]:
        status, response = _oauth_post(
            opener,
            endpoint + "device",
            {
                "client_id": DEVICE_CLIENT_ID,
                "scope": "openid",
            },
        )
        if status != 200:
            raise AssertionError(f"Device authorization returned HTTP {status}")
        return response

    def poll(code: str) -> tuple[int, dict[str, object]]:
        return _oauth_post(
            opener,
            endpoint + "token",
            {
                "grant_type": DEVICE_GRANT,
                "device_code": code,
                "client_id": DEVICE_CLIENT_ID,
            },
        )

    def decision(payload: dict[str, object], accept: bool) -> None:
        complete_uri = payload.get("verification_uri_complete")
        user_code = payload.get("user_code")
        if not isinstance(complete_uri, str) or not isinstance(user_code, str):
            raise AssertionError(
                "Device authorization omitted verification URI or user code"
            )

        def run_browser_flow() -> None:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    page = browser.new_page(ignore_https_errors=True)
                    page.goto(complete_uri, wait_until="domcontentloaded")
                    username_input = page.locator(
                        'input[type="email"], input[autocomplete="username"], '
                        'input[name="email"], input[type="text"]'
                    ).first
                    username_input.wait_for(state="visible", timeout=10_000)
                    username_input.fill(username)
                    _submit_password_step(page, password)
                    user_code_input = page.get_by_label("User Code", exact=True)
                    user_code_input.wait_for(state="visible", timeout=10_000)
                    user_code_input.fill(user_code)
                    page.get_by_role("button", name="Submit", exact=True).click()

                    names = (
                        r"accept|approve|allow|confirm|authorize|批准|允许"
                        if accept
                        else r"decline|deny|拒绝"
                    )
                    page.get_by_role("button", name=re.compile(names, re.I)).click(
                        timeout=30_000
                    )
                    message = (
                        r"The request has been accepted"
                        if accept
                        else r"The request has been declined"
                    )
                    page.get_by_text(re.compile(message, re.I)).wait_for(
                        state="visible", timeout=10_000
                    )
                finally:
                    browser.close()

        _run_device_browser_step(run_browser_flow)

    approved = start()
    code, lifetime, interval = (
        approved.get("device_code"),
        approved.get("expires_in"),
        approved.get("interval"),
    )
    if (
        not isinstance(code, str)
        or not isinstance(lifetime, int)
        or not isinstance(interval, int)
    ):
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
    id_token = response.get("id_token")
    if not isinstance(id_token, str):
        raise AssertionError("Device grant returned no ID token")
    verify_access_token(token, issuer)
    _device_public_api(gateway, token)
    print(
        "Device protocol: approved; RS256, default audience and "
        "five-minute token verified."
    )

    denied = start()
    denied_code = denied.get("device_code")
    if not isinstance(denied_code, str):
        raise AssertionError("Malformed denial device authorization response")
    decision(denied, False)
    time.sleep(interval + 1)
    status, response = poll(denied_code)
    denial_error = protocol_error(response)
    # Rauthy v0.37.2 deletes declined device codes; the token endpoint
    # reports expired_token.
    if status != 400 or denial_error != "expired_token":
        raise AssertionError(
            f"Denied device authorization returned HTTP {status}: {denial_error}"
        )
    print("Device protocol: user denial terminated without a token.")

    expired = start()
    expired_code, expired_lifetime = (
        expired.get("device_code"),
        expired.get("expires_in"),
    )
    if not isinstance(expired_code, str) or not isinstance(expired_lifetime, int):
        raise AssertionError("Malformed expiry device authorization response")
    print(f"Device protocol: waiting for natural expiry ({expired_lifetime}s).")
    time.sleep(expired_lifetime + 1)
    status, response = poll(expired_code)
    if status != 400 or protocol_error(response) != "expired_token":
        raise AssertionError("Expired device authorization did not terminate")
    print("Device protocol: expiry terminated without a token.")
    return token, id_token


def _device_public_api(gateway: str, token: str) -> None:
    opener = build_opener(ProxyHandler({}))
    headers = {"Authorization": f"Bearer {token}"}
    with opener.open(
        Request(gateway + "/api/v1/signals", headers=headers), timeout=20
    ) as response:
        if response.status != 200:
            raise AssertionError(f"Public signal API returned HTTP {response.status}")
    snapshot = {
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "holdings": {"510300": 100},
        "protected_floor": {"510300": 20},
        "sellable": {"510300": 80},
        "cost_basis": {"510300": "3.8500"},
        "available_cash": "125.50",
    }
    create = Request(
        gateway + "/api/v1/account-snapshot",
        data=json.dumps(snapshot).encode(),
        headers={
            **headers,
            "Content-Type": "application/json",
            "If-None-Match": "*",
        },
        method="PUT",
    )
    with opener.open(create, timeout=20) as response:
        if response.status not in (200, 201):
            raise AssertionError(
                f"Public snapshot creation returned HTTP {response.status}"
            )
    with opener.open(
        Request(gateway + "/api/v1/account-snapshot", headers=headers), timeout=20
    ) as response:
        if response.status != 200:
            raise AssertionError(
                f"Public snapshot read returned HTTP {response.status}"
            )
        etag = response.headers.get("ETag")
        saved = json.load(response)
    if not etag:
        raise AssertionError("Public snapshot read returned no ETag")
    if (
        saved.get("holdings") != {"510300": 100}
        or saved.get("available_cash") != "125.50"
    ):
        raise AssertionError("Public snapshot readback mismatch")
    snapshot["available_cash"] = "126.50"
    update = Request(
        gateway + "/api/v1/account-snapshot",
        data=json.dumps(snapshot).encode(),
        headers={
            **headers,
            "Content-Type": "application/json",
            "If-Match": etag,
        },
        method="PUT",
    )
    with opener.open(update, timeout=20) as response:
        if response.status not in (200, 201):
            raise AssertionError(
                f"Public snapshot write returned HTTP {response.status}"
            )
    with opener.open(
        Request(gateway + "/api/v1/account-snapshot", headers=headers), timeout=20
    ) as response:
        saved = json.load(response)
    if saved.get("available_cash") != "126.50":
        raise AssertionError("Public snapshot write was not visible to its owner")
    request = Request(
        gateway + "/api/v1/signals/stream?symbols=563360", headers=headers
    )
    with opener.open(request, timeout=20) as response:
        if (
            response.status != 200
            or response.headers.get_content_type() != "text/event-stream"
        ):
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
    if (
        not isinstance(iat, int)
        or isinstance(iat, bool)
        or not isinstance(exp, int)
        or isinstance(exp, bool)
        or exp - iat != 300
        or exp <= current
        or iat > current + 30
    ):
        raise AssertionError("Expected five-minute access token")


def internal_jwks_uri(uri: str) -> str:
    parsed = urlsplit(uri)
    if parsed.scheme != "https" or parsed.hostname != "localhost" or not parsed.path:
        raise ValueError("Expected Rauthy HTTPS localhost JWKS URL")
    return urlunsplit(("http", "rauthy:8080", parsed.path, parsed.query, ""))


def _compose_bootstrap_override(
    directory: Path,
    raft_secret: str,
    api_secret: str,
    rp_origin: str,
    admin_password: str,
) -> Path:
    override = directory / "compose.bootstrap.json"
    encryption_key_id = secrets.token_hex(4)
    encryption_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    override.write_text(
        json.dumps(
            {
                "services": {
                    "rauthy": {
                        "environment": {
                            "LOCAL_TEST": "false",
                            "BOOTSTRAP_DIR": "/app/bootstrap",
                            "BOOTSTRAP_ADMIN_PASSWORD_PLAIN": admin_password,
                            "HQL_SECRET_RAFT": raft_secret,
                            "HQL_SECRET_API": api_secret,
                            "HQL_NODE_ID": "1",
                            "ENC_KEYS": f"{encryption_key_id}/{encryption_key}",
                            "ENC_KEY_ACTIVE": encryption_key_id,
                            "RP_ID": "localhost",
                            "RP_ORIGIN": rp_origin,
                        },
                        "volumes": [f"{directory / 'bootstrap'}:/app/bootstrap:ro,U"],
                    },
                },
            }
        )
    )
    override.chmod(0o600)
    return override


def _rauthy_settings(port: str) -> dict[str, str]:
    return {
        "OIDC_ISSUER": f"https://localhost:{port}/auth/v1/",
        "RAUTHY_HOST_PORT": port,
        "RAUTHY_LISTEN_SCHEME": "http_https",
        "RAUTHY_PUB_URL": f"localhost:{port}",
    }


def _validate_discovery(issuer: str, discovery: dict[str, object]) -> None:
    if discovery.get("issuer") != issuer or not isinstance(
        discovery.get("jwks_uri"), str
    ):
        raise RuntimeError("Rauthy discovery issuer/JWKS mismatch")


def _write_bootstrap(
    bootstrap_dir: Path,
    gateway: str,
    username: str | None = None,
    password: str | None = None,
    api_secret: str | None = None,
    *,
    include_user_key: bool = False,
) -> tuple[str, str, str] | tuple[str, str, str, str]:
    bootstrap_dir.mkdir(mode=0o711, parents=True, exist_ok=True)
    bootstrap_dir.chmod(0o711)
    password = password or secrets.token_urlsafe(24)
    if username is None:
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
    else:
        users = [
            {
                "email": username,
                "given_name": "Acceptance",
                "family_name": "Tester",
                "password": {"Plain": password},
                "enabled": True,
                "email_verified": True,
                "roles": ["user"],
            }
        ]
    key_name = "acceptance-client-update" if api_secret else "a500-acceptance"
    api_secret = api_secret or secrets.token_hex(32)
    clients = [
        {
            "id": client_id,
            "name": client_name,
            "secret": None,
            "enabled": True,
            "redirect_uris": [gateway + "/"] if redirect else ["http://localhost/"],
            "allowed_origins": [gateway] if redirect else [],
            "flows_enabled": flows,
            "access_token_alg": "RS256",
            "id_token_alg": "RS256",
            "auth_code_lifetime": 60,
            "access_token_lifetime": 300,
            "scopes": ["openid"],
            "default_scopes": ["openid"],
            "force_mfa": False,
        }
        for client_id, client_name, redirect, flows in (
            (
                CLIENT_ID,
                "A500 browser acceptance",
                True,
                ["authorization_code", "refresh_token"],
            ),
            (
                DEVICE_CLIENT_ID,
                "A500 independent device acceptance",
                False,
                [DEVICE_GRANT],
            ),
        )
    ]
    client_access = [{"group": "Clients", "access_rights": ["update"]}]
    if username is None and not include_user_key:
        client_access = [
            {"group": "Clients", "access_rights": ["read", "create", "update"]},
            {"group": "Users", "access_rights": ["read", "update"]},
        ]
    api_keys = [
        {
            "name": key_name,
            "secret": {"Plain": api_secret},
            "exp": int(time.time()) + (1800 if username is None else 600),
            "access": client_access,
        }
    ]
    user_api_secret = None
    if include_user_key:
        user_api_secret = secrets.token_hex(32)
        api_keys.append(
            {
                "name": "a500-user-acceptance",
                "secret": {"Plain": user_api_secret},
                "exp": int(time.time()) + 1800,
                "access": [{"group": "Users", "access_rights": ["read", "update"]}],
            }
        )
    for filename, value in (
        ("users.json", users),
        ("clients.json", clients),
        ("api_keys.json", api_keys),
    ):
        path = bootstrap_dir / filename
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
    if include_user_key:
        assert user_api_secret is not None
        return key_name, api_secret, password, user_api_secret
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
            with urlopen(
                issuer.rstrip("/") + "/.well-known/openid-configuration",
                context=ctx,
                timeout=3,
            ) as response:
                result = json.load(response)
            _validate_discovery(issuer, result)
            return result
        except OSError, TimeoutError, json.JSONDecodeError:
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
        except json.JSONDecodeError, UnicodeDecodeError:
            raise AssertionError("Public API returned a non-JSON response") from None
        status = int(getattr(response, "status", response.code))
        return (
            status,
            {key.lower(): value for key, value in response.headers.items()},
            decoded,
        )
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
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="fills.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return multipart, f"multipart/form-data; boundary={boundary}"


def _assert_token_rejected(gateway: str, id_token: str) -> None:
    status, _, _ = _public_api_request(gateway, "/api/v1/signals", id_token)
    if status != 401:
        raise AssertionError(
            f"Public API accepted an ID token (HTTP {status}, expected 401)"
        )


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
            'input[autocomplete="username"], input[type="email"], '
            'input[name="login"], input[type="text"]'
        ).first
        username_field.wait_for(state="visible", timeout=30_000)
        username_field.fill(username)
        page.locator("form").get_by_role("button", name="Login", exact=True).click()
        password_field = page.locator('input[type="password"]:visible')
        password_field.wait_for(state="visible", timeout=30_000)
        password_field.fill(password)
        with page.expect_response(
            lambda response: (
                "/oidc/token" in urlsplit(response.url).path
                and response.request.method == "POST"
            ),
            timeout=60_000,
        ) as token_response:
            page.locator('button[type="submit"]:visible').click()
        page.wait_for_url(gateway + "/**", timeout=120_000)
        token_body = token_response.value.json()
        browser_token = token_body.get("access_token")
        refresh_token = token_body.get("refresh_token")
        if not isinstance(browser_token, str):
            raise AssertionError(
                "Browser OAuth response did not contain an access token"
            )
        if refresh_token is not None and not isinstance(refresh_token, str):
            raise AssertionError(
                "Browser OAuth response contained an invalid refresh token"
            )
        page.wait_for_function(
            "() => document.body.innerText.includes('策略信号')", timeout=60_000
        )
        verify_access_token(browser_token, issuer)
        status, _, api_error = _public_api_request(
            gateway, "/api/v1/signals", browser_token
        )
        if status != 200:
            raise AssertionError(
                "Browser access token was rejected by public API "
                f"(HTTP {status}: {api_error})"
            )

        if create_snapshot:
            page.get_by_role("link", name="资产管理", exact=True).click()
            page.get_by_role("button", name="创建快照").wait_for(timeout=30_000)

        if create_snapshot:
            page.get_by_role("button", name="创建快照").click()
            page.get_by_label("快照时间").fill(
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            )
            page.get_by_label("可用现金", exact=True).fill(cash)
            page.get_by_role("button", name="添加证券").click()
            page.get_by_label("第 1 行证券代码").fill("510300")
            page.get_by_label("第 1 行持有总份额").fill(str(holdings))
            page.get_by_label("第 1 行保护底仓").fill("20")
            page.get_by_label("第 1 行当前可卖").fill(str(holdings - 20))
            page.get_by_label("第 1 行成本基础").fill("3.8500")
            with page.expect_response(
                lambda r: (
                    r.url == gateway + "/api/v1/account-snapshot"
                    and r.request.method == "PUT"
                )
            ) as saved:
                page.get_by_role("button", name="创建账户快照", exact=True).click()
            if saved.value.status not in (200, 201):
                raise AssertionError(
                    f"Snapshot write returned HTTP {saved.value.status}"
                )
            page.wait_for_function(
                "() => document.body.innerText.includes('账户快照已保存')"
            )
            readback = page.evaluate(
                """async token => {
                    const r = await fetch('/api/v1/account-snapshot', {
                        headers: {Authorization: `Bearer ${token}`}
                    });
                    return {status: r.status, body: await r.json()};
                }""",
                browser_token,
            )
            if (
                readback["status"] != 200
                or readback["body"]["holdings"] != {"510300": holdings}
                or readback["body"]["available_cash"] != cash
            ):
                raise AssertionError(
                    "Snapshot readback did not match browser-written business data"
                )
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
        data=urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
            }
        ).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(
            request,
            context=ssl._create_unverified_context(),
            timeout=15,
        ) as response:
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
            page.locator(
                'input[autocomplete="username"], input[type="email"], '
                'input[name="login"], input[type="text"]'
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
                return (
                    page.url.startswith(issuer)
                    and page.locator('input[type="password"]:visible').count() > 0
                )
        finally:
            browser.close()


def _image_exists(image: str) -> bool:
    result = subprocess.run(
        ["podman", "image", "exists", image], check=False, timeout=10
    )
    return result.returncode == 0


def _cleanup_run(
    prefix: list[str],
    env: dict[str, str],
    project: str,
    disposable_images: list[str],
) -> None:
    teardown = subprocess.run(
        [*prefix, "down", "--volumes", "--remove-orphans", "--rmi", "local"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    resources = (
        (
            "containers",
            [
                "podman",
                "ps",
                "-aq",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            ["podman", "rm", "-f"],
        ),
        (
            "volumes",
            [
                "podman",
                "volume",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            ["podman", "volume", "rm", "-f"],
        ),
    )
    for resource, command, remove in resources:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=30
        )
        remaining = result.stdout.split()
        if remaining:
            subprocess.run([*remove, *remaining], check=True, timeout=60)
            result = subprocess.run(
                command, check=True, capture_output=True, text=True, timeout=30
            )
        if result.stdout.strip():
            raise RuntimeError(f"Test-created {resource} survived Compose teardown")
    result = subprocess.run(
        ["podman", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    project_images = [
        image
        for image in result.stdout.splitlines()
        if image.startswith(f"localhost/{project}_")
    ]
    for image in project_images:
        subprocess.run(["podman", "image", "rm", image], check=True, timeout=30)
    for image in disposable_images:
        if _image_exists(image):
            subprocess.run(["podman", "image", "rm", image], check=True, timeout=30)
    result = subprocess.run(
        ["podman", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if any(
        image.startswith(f"localhost/{project}_")
        for image in result.stdout.splitlines()
    ):
        raise RuntimeError("Test-created project images survived cleanup")
    remaining = [image for image in disposable_images if _image_exists(image)]
    if remaining:
        raise RuntimeError("Test-created pinned images survived cleanup")
    if teardown.returncode:
        raise RuntimeError("Compose teardown failed")


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    command = shlex.split(os.environ.get("PODMAN_COMPOSE", "podman compose"))
    if (
        not command
        or shutil.which(command[0]) is None
        or "podman" not in Path(command[0]).name
    ):
        raise RuntimeError("Podman Compose is required")
    project = "a500-rauthy-" + secrets.token_hex(6)
    mailpit_image = "docker.io/axllent/mailpit:v1.27.8"
    rauthy_image = "ghcr.io/sebadob/rauthy:0.37.2"
    preexisting_images = {
        image for image in (mailpit_image, rauthy_image) if _image_exists(image)
    }
    ports = {
        name: str(_port())
        for name in ("rauthy", "gateway", "postgres", "rabbit", "mailpit")
    }
    issuer = f"https://localhost:{ports['rauthy']}/auth/v1/"
    mailpit = f"http://127.0.0.1:{ports['mailpit']}"
    gateway = f"http://localhost:{ports['gateway']}"
    env = dict(os.environ)
    env.setdefault("UV_INDEX_URL", "https://mirrors.aliyun.com/pypi/simple/")
    for name in (
        "KEYCLOAK_ADMIN_USERNAME",
        "KEYCLOAK_ADMIN_PASSWORD",
        "AD_USERNAME",
        "AD_PASSWORD",
        "KEYCLOAK_HOSTNAME",
        "KEYCLOAK_HOST_PORT",
        "TLS_CERT",
        "TLS_KEY",
    ):
        env.pop(name, None)
    temp = tempfile.TemporaryDirectory(prefix=project + "-")
    Path(temp.name).chmod(0o711)
    bootstrap_dir = Path(temp.name) / "bootstrap"
    encryption_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    subprocess.run(
        ["uv", "build", "--wheel"], cwd=ROOT, env=env, check=True, timeout=120
    )
    env.update(
        {
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
            "MAILPIT_HOST_PORT": ports["mailpit"],
            "SMTP_USERNAME": "",
            "SMTP_PASSWORD": "",
            "RAUTHY_ENC_KEYS": "test/" + encryption_key,
            "RAUTHY_ENC_KEY_ACTIVE": "test",
            **_rauthy_settings(ports["rauthy"]),
        }
    )
    files = [
        "compose.yaml",
        "compose.web.yaml",
        "compose.web-http.yaml",
        "compose.rauthy.yaml",
    ]
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
        (
            api_key_name,
            api_key_secret,
            user_password,
            user_api_key_secret,
        ) = _write_bootstrap(bootstrap_dir, gateway, include_user_key=True)
        admin_password = secrets.token_urlsafe(24)
        override = _compose_bootstrap_override(
            Path(temp.name),
            env["RAUTHY_SECRET_RAFT"],
            env["RAUTHY_SECRET_API"],
            f"https://localhost:{ports['rauthy']}",
            admin_password,
        )
        prefix.extend(("-f", str(override)))
        subprocess.run(
            [*prefix, "up", "-d", "rauthy"],
            cwd=ROOT,
            env=env,
            check=True,
            timeout=180,
        )
        discovery = _wait_discovery(issuer)
        _set_client_audience(issuer, api_key_name, api_key_secret)
        env["OIDC_JWKS_URI"] = internal_jwks_uri(str(discovery["jwks_uri"]))
        subprocess.run(
            [
                *prefix,
                "up",
                "-d",
                "signal-api",
                "account-api",
                "jobs-api",
                "frontend",
                "gateway",
            ],
            cwd=ROOT,
            env=env,
            check=True,
            timeout=300,
        )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                with urlopen(gateway + "/", timeout=3):
                    break
            except OSError:
                time.sleep(2)
        else:
            raise RuntimeError("Public gateway did not become ready")
        print(f"Disposable Mailpit inbox: {mailpit}")
        browser_token, browser_refresh_token = _browser_flow(
            gateway,
            issuer,
            str(discovery["token_endpoint"]),
            "browser@example.test",
            user_password,
        )
        device_token, device_id_token = _device_grant(
            gateway, issuer, "device@example.test", user_password
        )
        device_token, _ = _browser_flow(
            gateway,
            issuer,
            str(discovery["token_endpoint"]),
            "device@example.test",
            user_password,
            create_snapshot=False,
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
                raise AssertionError(
                    "Public API accepted an absent or malformed bearer token"
                )
        if (
            _public_api_request(gateway, "/api/v1/account-snapshot", device_token)[0]
            == 200
        ):
            raise AssertionError(
                "Second Rauthy user could read the browser user's account snapshot"
            )
        if (
            _public_api_request(gateway, "/api/v1/account-snapshot", admin_token)[0]
            == 200
        ):
            raise AssertionError(
                "Rauthy identity admin could read the browser user's account snapshot"
            )
        if (
            _public_api_request(gateway, "/api/v1/account-snapshot", browser_token)[0]
            != 200
        ):
            raise AssertionError(
                "Browser user's account snapshot was not available to its owner"
            )
        original_subject = _decode(browser_token.split(".")[1]).get("sub")
        changed_email = "browser-renamed@example.test"
        recovered_password = secrets.token_urlsafe(24)
        if (
            _change_user_email(
                issuer,
                "a500-user-acceptance",
                user_api_key_secret,
                "browser@example.test",
                changed_email,
            )
            != original_subject
        ):
            raise AssertionError(
                "Rauthy changed the user's subject during email update"
            )
        renamed_token, _ = _browser_flow(
            gateway,
            issuer,
            str(discovery["token_endpoint"]),
            changed_email,
            user_password,
            create_snapshot=False,
        )
        if _decode(renamed_token.split(".")[1]).get("sub") != original_subject:
            raise AssertionError("Email login changed the authenticated subject")
        if (
            _public_api_request(gateway, "/api/v1/account-snapshot", renamed_token)[0]
            != 200
        ):
            raise AssertionError("Original business data was lost after email update")
        _perform_password_recovery(issuer, mailpit, changed_email, recovered_password)
        recovered_token, recovered_refresh_token = _browser_flow(
            gateway,
            issuer,
            str(discovery["token_endpoint"]),
            changed_email,
            recovered_password,
            create_snapshot=False,
        )
        if _decode(recovered_token.split(".")[1]).get("sub") != original_subject:
            raise AssertionError(
                "Recovered-password login changed the authenticated subject"
            )
        if (
            _public_api_request(gateway, "/api/v1/account-snapshot", recovered_token)[0]
            != 200
        ):
            raise AssertionError(
                "Recovered-password login could not access existing business data"
            )
        browser_token = renamed_token
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
            raise AssertionError(
                f"Browser user trade-import preview returned HTTP {status}"
            )
        batch_id = imported.get("batch_id")
        if not isinstance(batch_id, str):
            raise AssertionError(
                "Trade-import preview did not return a batch identifier"
            )
        preview_path = f"/api/v1/trade-imports/previews/{batch_id}"
        if _public_api_request(gateway, preview_path, browser_token)[0] != 200:
            raise AssertionError("Trade-import preview was not visible to its owner")
        if _public_api_request(gateway, preview_path, device_token)[0] != 404:
            raise AssertionError(
                "Second Rauthy user could read the first user's trade-import preview"
            )
        if (
            _public_api_request(
                gateway, preview_path + "/confirm", device_token, method="POST"
            )[0]
            != 404
        ):
            raise AssertionError(
                "Second Rauthy user could confirm the first user's trade-import preview"
            )
        browser_token, browser_refresh_token = recovered_token, recovered_refresh_token
        if not browser_refresh_token:
            raise AssertionError(
                "Rauthy did not issue the recovered user's browser refresh credential"
            )
        token_exp = _decode(browser_token.split(".")[1]).get("exp")
        if not isinstance(token_exp, int):
            raise AssertionError("Browser access token has no expiry")
        _disable_user(
            issuer, "a500-user-acceptance", user_api_key_secret, changed_email
        )
        if _public_api_request(gateway, "/api/v1/signals", browser_token)[0] != 200:
            raise AssertionError(
                "Previously issued token did not remain valid after user disable"
            )
        if _refresh_token(
            str(discovery["token_endpoint"]), CLIENT_ID, browser_refresh_token
        ) not in (400, 401):
            raise AssertionError("Rauthy accepted refresh after the user was disabled")
        if not _new_login_rejected(
            gateway,
            issuer,
            changed_email,
            recovered_password,
        ):
            raise AssertionError(
                "Rauthy accepted a new browser login after the user was disabled"
            )
        wait_seconds = max(0, token_exp - int(time.time())) + 1
        print(
            "Disabled disposable browser user: prior token remains locally valid; "
            f"waiting {wait_seconds}s for its five-minute expiry."
        )
        time.sleep(wait_seconds)
        expiry_status = _public_api_request(gateway, "/api/v1/signals", browser_token)[
            0
        ]
        if expiry_status != 401:
            raise AssertionError(
                "Previously issued access token returned "
                f"HTTP {expiry_status} after expiry"
            )
        print(
            "Verified disabled-user login and refresh rejection; prior access token "
            "was accepted during its residual window and rejected after expiry."
        )
        print(
            "Verified real browser and device access tokens, ID-token rejection, "
            "and public API snapshot/trade-import subject isolation."
        )
    finally:
        try:
            _cleanup_run(
                prefix,
                env,
                project,
                [
                    image
                    for image in (mailpit_image, rauthy_image)
                    if image not in preexisting_images
                ],
            )
        finally:
            temp.cleanup()


if __name__ == "__main__":
    main()
