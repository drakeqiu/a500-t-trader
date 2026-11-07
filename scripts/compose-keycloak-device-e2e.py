from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shlex
import socket
import ssl
import subprocess
import tempfile
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
TLS_CONTEXT: ssl.SSLContext | None = None


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
    body = (
        json.dumps(json_data).encode()
        if json_data is not None
        else (urlencode(data).encode() if data is not None else None)
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
        with urlopen(request, timeout=10, context=TLS_CONTEXT) as response:
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
    status, clients = _request(
        f"{url}?clientId=a500-browser-client", bearer=admin_token
    )
    if status != 200 or not isinstance(clients, list) or len(clients) != 1:
        raise RuntimeError("无法查询浏览器客户端")
    client = clients[0]
    client["webOrigins"] = [origin]
    client["redirectUris"] = [f"{origin}/"]
    status, _ = _request(
        f"{url}/{client['id']}", bearer=admin_token, method="PUT", json_data=client
    )
    if status != 204:
        raise RuntimeError(f"无法配置浏览器客户端回调地址: HTTP {status}")


def _check_browser_login(
    gateway_url: str, username: str, password: str, *, https: bool = False
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(ignore_https_errors=https)
            page.goto(gateway_url, wait_until="domcontentloaded", timeout=30000)
            page.get_by_role("button", name="安全连接", exact=True).click()
            page.locator("input[name='username']").fill(username, timeout=30000)
            page.locator("input[name='password']").fill(password)
            page.locator("button[type='submit'], input[type='submit']").first.click()
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
        "/v1/signals",
        "/v1/account-snapshot",
        "/web-config",
        "/web/app.js",
        "/web/app.css",
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
    with urlopen(request, timeout=10, context=TLS_CONTEXT) as response:
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
                    *compose,
                    "exec",
                    "-T",
                    "redis",
                    "redis-cli",
                    "XADD",
                    "a500:signals:events",
                    "*",
                    "symbol",
                    "563360",
                    "type",
                    "BUY",
                    "data",
                    payload,
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


def _make_test_certificate(directory: Path) -> tuple[Path, Path]:
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return cert, key


def _check_binding(
    compose: list[str], environment: dict[str, str], host: str, port: int
) -> None:
    def published(service: str, container_port: int) -> str:
        if "podman" in Path(compose[0]).name:
            project = compose[compose.index("--project-name") + 1]
            container = subprocess.run(
                [
                    "podman",
                    "ps",
                    "-q",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                    "--filter",
                    f"label=com.docker.compose.service={service}",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout.strip()
            if not container or "\n" in container:
                raise RuntimeError(f"无法唯一确定 {service} 容器")
        else:
            container = subprocess.run(
                [*compose, "ps", "-q", service],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout.strip()
            if not container or "\n" in container:
                raise RuntimeError(f"无法唯一确定 {service} 容器")
        runtime = "podman" if "podman" in Path(compose[0]).name else "docker"
        result = subprocess.run(
            [
                runtime,
                "inspect",
                "--format",
                "{{json .NetworkSettings.Ports}}",
                container,
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        ports = json.loads(result.stdout) or {}
        return "\n".join(
            f"{item['HostIp']}:{item['HostPort']}"
            for item in (ports.get(f"{container_port}/tcp") or [])
        )

    binding = published("gateway", 8080)
    if binding != f"{host}:{port}":
        raise AssertionError(f"网关监听地址不符: {binding!r}")
    for service, container_port in (
        ("frontend", 3000),
        ("signal-api", 8000),
        ("account-api", 8000),
    ):
        if published(service, container_port):
            raise AssertionError(f"{service} 不应发布主机端口")


def _check_no_plaintext(port: int) -> None:
    try:
        status, _ = _request(f"http://localhost:{port}/api/v1/signals")
    except OSError, URLError:
        return
    if status in (200, 401):
        raise AssertionError("HTTPS 入口接受了明文 API 请求")


def main(*, mock_market_data: bool = False) -> None:
    global TLS_CONTEXT
    required = (
        "AMAZINGDATA_USERNAME",
        "AMAZINGDATA_PASSWORD",
        "AMAZINGDATA_HOST",
        "AMAZINGDATA_PORT",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing and not mock_market_data:
        raise RuntimeError(
            "真实账户 API 验收需要可用的 AmazingData 服务和环境变量: "
            + ", ".join(missing)
        )
    project = f"a500-keycloak-e2e-{secrets.token_hex(6)}"
    keycloak_port = _available_port()
    admin_name = f"e2e-admin-{secrets.token_hex(8)}"
    admin_password = secrets.token_urlsafe(32)
    user_name = f"e2e-user-{secrets.token_hex(8)}"
    user_password = secrets.token_urlsafe(32)
    other_name = f"e2e-user-{secrets.token_hex(8)}"
    other_password = secrets.token_urlsafe(32)
    base_url = f"http://localhost:{keycloak_port}"
    environment = {
        **os.environ,
        "KEYCLOAK_ADMIN_USERNAME": admin_name,
        "KEYCLOAK_ADMIN_PASSWORD": admin_password,
        "KEYCLOAK_HOSTNAME": base_url,
        "KEYCLOAK_HOST_PORT": str(keycloak_port),
        "A500_POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "A500_POSTGRES_PORT": str(_available_port()),
        "A500_OIDC_ISSUER": f"{base_url}/realms/{REALM}",
        "A500_OIDC_AUDIENCE": API_AUDIENCE,
        "A500_OIDC_JWKS_URI": (
            f"http://keycloak:8080/realms/{REALM}/protocol/openid-connect/certs"
        ),
        "A500_MARKET_DATA_MODE": "replay",
        # Only an explicit test-mode override may replace the live provider.
    }
    if mock_market_data:
        print("验收使用测试专用模拟行情，非真实 TGW 行情", flush=True)
        for name in required:
            environment[name] = "test-only-not-used"
        environment["A500_E2E_MOCK_MARKET_SCRIPT"] = str(
            ROOT / "scripts/fixtures/account-api-mock-market.py"
        )
    for name in (
        "A500_GATEWAY_HOST",
        "A500_GATEWAY_HOST_PORT",
        "A500_TLS_CERT",
        "A500_TLS_KEY",
    ):
        environment.pop(name, None)
    compose_command = shlex.split(
        os.environ.get("A500_COMPOSE_COMMAND")
        or os.environ.get("PODMAN_COMPOSE")
        or "docker compose"
    )
    common = [
        *compose_command,
        "--env-file",
        os.devnull,
        "--project-name",
        project,
        "-f",
        str(ROOT / "compose.yaml"),
        "-f",
        str(ROOT / "compose.keycloak.yaml"),
        "-f",
        str(ROOT / "compose.web.yaml"),
    ]
    if mock_market_data:
        common.extend(
            ["-f", str(ROOT / "scripts/fixtures/compose-account-mock-market.yaml")]
        )
    compose = [*common, "-f", str(ROOT / "compose.web-http.yaml")]
    with tempfile.TemporaryDirectory(prefix="a500-e2e-tls-") as directory:
        cert, key = _make_test_certificate(Path(directory))
        environment.update(A500_TLS_CERT=str(cert), A500_TLS_KEY=str(key))
        TLS_CONTEXT = ssl.create_default_context(cafile=str(cert))
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
                    "frontend",
                    "gateway",
                ],
                cwd=ROOT,
                env=environment,
                check=True,
                timeout=600,
            )
            _wait_for_keycloak(base_url)
            admin_token = _admin_token(base_url, admin_name, admin_password)
            _create_temporary_user(base_url, admin_token, user_name, user_password)
            _create_temporary_user(base_url, admin_token, other_name, other_password)
            token = _device_authorize(base_url, user_name, user_password)
            other_token = _device_authorize(base_url, other_name, other_password)

            scenarios = [
                ("http", "127.0.0.1", 8080),
                ("http", "0.0.0.0", _available_port()),
                ("https", "127.0.0.1", 8443),
                ("https", "0.0.0.0", _available_port()),
            ]
            for index, (scheme, host, port) in enumerate(scenarios):
                if index:
                    compose = [*common, "-f", str(ROOT / f"compose.web-{scheme}.yaml")]
                    if host == "127.0.0.1":
                        environment.pop("A500_GATEWAY_HOST", None)
                        environment.pop("A500_GATEWAY_HOST_PORT", None)
                    else:
                        environment.update(
                            A500_GATEWAY_HOST=host, A500_GATEWAY_HOST_PORT=str(port)
                        )
                    subprocess.run(
                        [
                            *compose,
                            "up",
                            "-d",
                            "--no-deps",
                            "--force-recreate",
                            "gateway",
                        ],
                        cwd=ROOT,
                        env=environment,
                        check=True,
                        timeout=120,
                    )
                _check_binding(compose, environment, host, port)
                url = f"{scheme}://localhost:{port}"
                if scheme == "https":
                    _check_no_plaintext(port)
                _allow_browser_origin(base_url, admin_token, url)
                _check_browser_login(
                    url, user_name, user_password, https=scheme == "https"
                )
                _check_api(
                    url,
                    token,
                    other_token,
                    create=index == 0,
                    compose=compose,
                    environment=environment,
                )

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
            _check_api(
                url,
                token,
                other_token,
                create=False,
                compose=compose,
                environment=environment,
            )
        finally:
            try:
                subprocess.run(
                    [*compose, "down", "--volumes", "--remove-orphans"],
                    cwd=ROOT,
                    env=environment,
                    check=True,
                    timeout=120,
                )
            finally:
                TLS_CONTEXT = None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="验收真实 Keycloak、账户 API、网关和浏览器链路"
    )
    parser.add_argument(
        "--mock-market-data",
        action="store_true",
        help="显式使用测试专用模拟行情（CI；不验证真实 TGW）",
    )
    main(mock_market_data=parser.parse_args().mock_market_data)
