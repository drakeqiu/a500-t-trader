from __future__ import annotations

import base64
import json
import os
import secrets
import shlex
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
REALM = "a500"
CLIENT_ID = "a500-device-client"
API_AUDIENCE = "a500-api"


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(
    url: str,
    *,
    data: dict[str, str] | None = None,
    bearer: str | None = None,
    json_data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
) -> tuple[int, dict[str, Any] | str]:
    body = json.dumps(json_data).encode() if json_data is not None else (
        urlencode(data).encode() if data is not None else None
    )
    request_headers = dict(headers or {})
    if json_data is not None:
        request_headers["Content-Type"] = "application/json"
    elif data is not None:
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    if bearer:
        request_headers["Authorization"] = f"Bearer {bearer}"
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            raw = response.read().decode()
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, raw
    except HTTPError as error:
        raw = error.read().decode()
        try:
            return error.code, json.loads(raw)
        except json.JSONDecodeError:
            return error.code, raw


def _wait_for_keycloak(base_url: str) -> None:
    deadline = time.monotonic() + 180
    url = f"{base_url}/realms/{REALM}/.well-known/openid-configuration"
    while time.monotonic() < deadline:
        try:
            status, _ = _request(url)
            if status == 200:
                return
        except TimeoutError, URLError, OSError:
            pass
        time.sleep(1)
    raise RuntimeError("临时 Keycloak 未在 180 秒内就绪")


def _admin_token(base_url: str, username: str, password: str) -> str:
    status, result = _request(
        f"{base_url}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": username,
            "password": password,
        },
    )
    if status != 200 or not isinstance(result, dict) or "access_token" not in result:
        raise RuntimeError(f"无法登录临时 Keycloak 管理员: HTTP {status}")
    return str(result["access_token"])


def _create_temporary_user(
    base_url: str, admin_token: str, username: str, password: str
) -> None:
    # Keycloak 管理 REST 接受 JSON；生成的凭据只保留在内存中。
    body = json.dumps(
        {
            "username": username,
            "email": f"{username}@example.test",
            "emailVerified": True,
            "firstName": "Device",
            "lastName": "Flow",
            "enabled": True,
            "credentials": [
                {"type": "password", "value": password, "temporary": False}
            ],
        }
    ).encode()
    request = Request(
        f"{base_url}/admin/realms/{REALM}/users",
        data=body,
        headers={
            "Authorization": f"Bearer {admin_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            status = response.status
    except HTTPError as error:
        status = error.code
    if status != 201:
        raise RuntimeError(f"无法创建隔离测试用户: HTTP {status}")

def _allow_browser_origin(base_url: str, admin_token: str, origin: str) -> None:
    url = f"{base_url}/admin/realms/{REALM}/clients"
    status, clients = _request(f"{url}?clientId={CLIENT_ID}", bearer=admin_token)
    if status != 200 or not isinstance(clients, list) or len(clients) != 1:
        raise RuntimeError("无法查询设备客户端")
    client = clients[0]
    client["webOrigins"] = [origin]
    status, _ = _request(
        f"{url}/{client['id']}", bearer=admin_token, method="PUT", json_data=client
    )
    if status != 204:
        raise RuntimeError(f"无法配置设备客户端浏览器来源: HTTP {status}")


def _check_browser_login(gateway_url: str, username: str, password: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(gateway_url, wait_until="domcontentloaded", timeout=30000)
            page.get_by_role("button", name="安全连接", exact=True).click()
            page.locator(".user-code").wait_for(state="visible", timeout=30000)
            code = page.locator(".user-code").inner_text()
            with page.expect_popup() as popup_info:
                page.get_by_role("link", name="打开验证页面").click()
            popup = popup_info.value
            popup.wait_for_load_state("domcontentloaded", timeout=30000)
            code_field = popup.locator("input[name='user_code']")
            if code_field.count() and code_field.is_visible():
                code_field.fill(code)
                popup.locator("button[type='submit'], input[type='submit']").first.click()
            popup.locator("input[name='username']").fill(username, timeout=30000)
            popup.locator("input[name='password']").fill(password)
            popup.locator("button[type='submit'], input[type='submit']").first.click()
            popup.locator("#kc-oauth").wait_for(state="visible", timeout=30000)
            popup.locator("#kc-login").click()
            page.get_by_role("heading", name="实时信号", exact=True).wait_for(
                state="visible", timeout=90000
            )
        finally:
            browser.close()


def _device_authorize(base_url: str, username: str, password: str) -> str:
    status, result = _request(
        f"{base_url}/realms/{REALM}/protocol/openid-connect/auth/device",
        data={"client_id": CLIENT_ID, "scope": "openid"},
    )
    if status != 200 or not isinstance(result, dict):
        raise RuntimeError(f"设备授权端点拒绝请求: HTTP {status}")
    device_code = str(result["device_code"])
    user_code = str(result["user_code"])
    page_url = str(
        result.get("verification_uri_complete") or result["verification_uri"]
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            code_field = page.locator("input[name='user_code']")
            if code_field.count() and code_field.is_visible():
                code_field.fill(user_code)
                (
                    page.locator(
                        "button[type='submit'], input[type='submit']"
                    ).first.click()
                )
            username_field = page.locator("input[name='username']")
            username_field.wait_for(state="visible", timeout=30000)
            username_field.fill(username)
            page.locator("input[name='password']").fill(password)
            page.locator("button[type='submit'], input[type='submit']").first.click()
            approval = page.locator("#kc-oauth")
            approval.wait_for(state="visible", timeout=30000)
            page.locator("#kc-login").click()
        finally:
            browser.close()

    token_url = f"{base_url}/realms/{REALM}/protocol/openid-connect/token"
    interval = max(1, int(result.get("interval", 5)))
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        status, token_result = _request(
            token_url,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": CLIENT_ID,
            },
        )
        if status == 200 and isinstance(token_result, dict):
            token = str(token_result["access_token"])
            payload = token.split(".")[1]
            claims = json.loads(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            )
            audiences = claims["aud"]
            if API_AUDIENCE not in (
                audiences if isinstance(audiences, list) else [audiences]
            ):
                raise AssertionError("Keycloak access token 缺少 a500-api audience")
            return token
        if not isinstance(token_result, dict):
            raise RuntimeError(f"设备令牌轮询失败: HTTP {status}")
        error = token_result.get("error")
        if error == "authorization_pending":
            time.sleep(interval)
            continue
        if error == "slow_down":
            interval += 5
            time.sleep(interval)
            continue
        raise RuntimeError(f"设备授权未完成: {error or status}")
    raise RuntimeError("Playwright 验证后仍未取得设备访问令牌")


def _check_api(
    base_url: str,
    token: str,
    other_token: str,
    *,
    create: bool,
    compose: list[str],
    environment: dict[str, str],
) -> None:
    for path in (
        "/api/v1/signals",
        "/api/v1/account-snapshot",
        "/api/v1/signals/stream?symbols=563360",
    ):
        status, _ = _request(f"{base_url}{path}")
        if status != 401:
            raise AssertionError(f"无令牌请求 {path} 应为 401，实际为 {status}")
    status, _ = _request(f"{base_url}/api/v1/signals", bearer=token)
    if status != 200:
        raise AssertionError(f"有效 Keycloak 令牌访问 API 应为 200，实际为 {status}")
    for path in (
        "/v1/signals", "/v1/account-snapshot", "/web-config",
        "/web/app.js", "/web/app.css",
    ):
        status, _ = _request(f"{base_url}{path}", bearer=token)
        if status != 404:
            raise AssertionError(f"已移除入口 {path} 应为 404，实际为 {status}")
    status, _ = _request(base_url + "/")
    if status != 200:
        raise AssertionError(f"网关 UI 应为 200，实际为 {status}")
    account_url = f"{base_url}/api/v1/account-snapshot"
    if create:
        status, _ = _request(
            account_url,
            bearer=token,
            method="PUT",
            headers={"If-None-Match": "*"},
            json_data={
                "as_of": "2026-11-04T09:30:00+08:00",
                "holdings": {"510300": 100},
                "protected_floor": {"510300": 20},
                "sellable": {"510300": 80},
                "cost_basis": {"510300": "3.8500"},
                "available_cash": "125.50",
            },
        )
        if status != 201:
            raise AssertionError(f"创建账户快照应为 201，实际为 {status}")
    status, snapshot = _request(account_url, bearer=token)
    if (
        status != 200
        or not isinstance(snapshot, dict)
        or snapshot.get("holdings") != {"510300": 100}
    ):
        raise AssertionError("账户所有者无法读取自己的快照")
    status, _ = _request(account_url, bearer=other_token)
    if status != 404:
        raise AssertionError(f"其他账户访问快照应为 404，实际为 {status}")
    request = Request(
        f"{base_url}/api/v1/signals/stream?symbols=563360",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urlopen(request, timeout=10) as response:
        if response.status != 200 or response.headers.get_content_type() != (
            "text/event-stream"
        ):
            raise AssertionError("SSE 响应状态或内容类型错误")
        if response.readline().decode().strip() != "event: snapshot":
            raise AssertionError("SSE 未收到初始快照事件")
        while response.readline().strip():
            pass
        for _ in range(2):
            time.sleep(1)
            marker = secrets.token_hex(8)
            payload = json.dumps(
                {"symbol": "563360", "status": "BUY", "marker": marker}
            )
            subprocess.run(
                [
                    *compose, "exec", "-T", "redis", "redis-cli", "XADD",
                    "a500:signals:events", "*", "symbol", "563360", "type", "BUY",
                    "data", payload,
                ],
                cwd=ROOT,
                env=environment,
                check=True,
                timeout=10,
                capture_output=True,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                line = response.readline()
                if not line:
                    raise AssertionError("SSE 在后续事件之前断开")
                if marker.encode() in line:
                    break
            else:
                raise AssertionError("SSE 未及时收到后续事件")


def _verify_user_persisted(base_url: str, admin_token: str, username: str) -> None:
    status, users = _request(
        f"{base_url}/admin/realms/{REALM}/users?username={username}&exact=true",
        bearer=admin_token,
    )
    if (
        status != 200
        or not isinstance(users, list)
        or not any(user.get("username") == username for user in users)
    ):
        raise AssertionError("Keycloak 重启后 Realm 中未找到临时测试用户")


def main() -> None:
    project = f"a500-keycloak-e2e-{secrets.token_hex(6)}"
    keycloak_port = _available_port()
    gateway_port = _available_port()
    admin_name = f"e2e-admin-{secrets.token_hex(8)}"
    admin_password = secrets.token_urlsafe(32)
    user_name = f"e2e-user-{secrets.token_hex(8)}"
    user_password = secrets.token_urlsafe(32)
    other_name = f"e2e-user-{secrets.token_hex(8)}"
    other_password = secrets.token_urlsafe(32)
    public_url = f"http://localhost:{keycloak_port}"
    base_url = public_url
    api_url = f"http://127.0.0.1:{gateway_port}"
    environment = {
        **os.environ,
        "KEYCLOAK_ADMIN_USERNAME": admin_name,
        "KEYCLOAK_ADMIN_PASSWORD": admin_password,
        "KEYCLOAK_HOSTNAME": public_url,
        "KEYCLOAK_HOST_PORT": str(keycloak_port),
        "A500_GATEWAY_HOST_PORT": str(gateway_port),
        "A500_OIDC_ISSUER": f"{public_url}/realms/{REALM}",
        "A500_OIDC_AUDIENCE": API_AUDIENCE,
        "A500_OIDC_JWKS_URI": (
            f"http://keycloak:8080/realms/{REALM}/protocol/openid-connect/certs"
        ),
        "A500_MARKET_DATA_MODE": "replay",
        "AMAZINGDATA_USERNAME": "isolated-device-flow-test",
        "AMAZINGDATA_PASSWORD": secrets.token_urlsafe(32),
        "AMAZINGDATA_HOST": "127.0.0.1",
        "AMAZINGDATA_PORT": "1",
    }
    compose_command = shlex.split(
        os.environ.get("A500_COMPOSE_COMMAND")
        or os.environ.get("PODMAN_COMPOSE")
        or "docker compose"
    )
    compose = [
        *compose_command,
        "--project-name",
        project,
        "-f",
        str(ROOT / "compose.yaml"),
        "-f",
        str(ROOT / "compose.keycloak.yaml"),
        "-f",
        str(ROOT / "compose.web.yaml"),
    ]
    try:
        subprocess.run(
            [
                *compose,
                "up",
                "-d",
                "--build",
                "keycloak",
                "signal-api",
                "account-api",
                "gateway",
            ],
            cwd=ROOT,
            env=environment,
            check=True,
            timeout=600,
        )
        _wait_for_keycloak(base_url)
        admin_token = _admin_token(base_url, admin_name, admin_password)
        _allow_browser_origin(base_url, admin_token, f"http://localhost:{gateway_port}")
        _create_temporary_user(base_url, admin_token, user_name, user_password)
        _create_temporary_user(base_url, admin_token, other_name, other_password)
        _check_browser_login(f"http://localhost:{gateway_port}", user_name, user_password)
        token = _device_authorize(base_url, user_name, user_password)
        other_token = _device_authorize(base_url, other_name, other_password)
        _check_api(api_url, token, other_token, create=True, compose=compose, environment=environment)

        subprocess.run(
            [*compose, "restart", "keycloak"],
            cwd=ROOT,
            env=environment,
            check=True,
            timeout=120,
        )
        _wait_for_keycloak(base_url)
        admin_token = _admin_token(base_url, admin_name, admin_password)
        _verify_user_persisted(base_url, admin_token, user_name)
        token = _device_authorize(base_url, user_name, user_password)
        other_token = _device_authorize(base_url, other_name, other_password)
        _check_api(api_url, token, other_token, create=False, compose=compose, environment=environment)
    finally:
        subprocess.run(
            [*compose, "down", "--volumes", "--remove-orphans"],
            cwd=ROOT,
            env=environment,
            check=True,
            timeout=120,
        )


if __name__ == "__main__":
    main()
