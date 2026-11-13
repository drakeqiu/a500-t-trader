from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "start-local.py"
SPEC = importlib.util.spec_from_file_location("start_local", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
LOCAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LOCAL)


def test_startup_services_include_history_worker_and_dispatcher() -> None:
    services = LOCAL.SERVICES

    assert "jobs-api" in services
    assert "jobs-worker" in services
    assert "history-dispatcher" in services
    assert services.index("jobs-api") < services.index("gateway")


def test_local_environment_keeps_provider_configuration_ad_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        "AD_USERNAME=trader\nAD_PASSWORD=secret\nAD_HOST=market.example\nAD_PORT=9876\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(LOCAL, "ROOT", tmp_path)
    for name in ("USERNAME", "PASSWORD", "HOST", "PORT"):
        monkeypatch.delenv(f"AD_{name}", raising=False)
        monkeypatch.delenv(f"AMAZINGDATA_{name}", raising=False)

    environment = LOCAL.local_environment()

    assert environment["AD_USERNAME"] == "trader"
    assert environment["AD_PORT"] == "9876"
    assert not any(name.startswith("AMAZINGDATA_") for name in environment)


def test_local_environment_enables_history_persistence_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(LOCAL, "ROOT", tmp_path)
    monkeypatch.delenv("HISTORY_PERSISTENCE_PERMITTED", raising=False)

    assert LOCAL.local_environment()["HISTORY_PERSISTENCE_PERMITTED"] == "true"


def test_local_environment_preserves_explicit_persistence_opt_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(LOCAL, "ROOT", tmp_path)
    monkeypatch.setenv("HISTORY_PERSISTENCE_PERMITTED", "false")

    assert LOCAL.local_environment()["HISTORY_PERSISTENCE_PERMITTED"] == "false"


def test_local_environment_defaults_build_mirrors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UV_INDEX_URL", raising=False)
    monkeypatch.delenv("APT_MIRROR", raising=False)
    monkeypatch.delenv("UV_HTTP_TIMEOUT", raising=False)

    environment = LOCAL.local_environment()

    assert environment["UV_INDEX_URL"] == "https://mirrors.aliyun.com/pypi/simple/"
    assert environment["UV_HTTP_TIMEOUT"] == "180"
    assert environment["APT_MIRROR"] == "mirrors.bfsu.edu.cn"


def test_local_environment_defaults_empty_build_mirrors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UV_INDEX_URL", "")
    monkeypatch.setenv("APT_MIRROR", "")
    monkeypatch.setenv("UV_HTTP_TIMEOUT", "")

    environment = LOCAL.local_environment()

    assert environment["UV_INDEX_URL"] == "https://mirrors.aliyun.com/pypi/simple/"
    assert environment["UV_HTTP_TIMEOUT"] == "180"
    assert environment["APT_MIRROR"] == "mirrors.bfsu.edu.cn"


def test_local_environment_defaults_to_single_rauthy_issuer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(LOCAL, "ROOT", tmp_path)

    environment = LOCAL.local_environment()

    assert environment["OIDC_ISSUER"] == "http://localhost:8081/auth/v1/"
    assert environment["OIDC_JWKS_URI"] == "http://rauthy:8080/auth/v1/oidc/certs"
    assert environment["OIDC_CLIENT_ID"] == "a500-browser-client"


def test_local_environment_preserves_configured_build_mirrors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UV_INDEX_URL", "https://pypi.example/simple/")
    monkeypatch.setenv("APT_MIRROR", "apt.example")
    monkeypatch.setenv("UV_HTTP_TIMEOUT", "90")
    environment = LOCAL.local_environment()

    assert environment["UV_INDEX_URL"] == "https://pypi.example/simple/"
    assert environment["UV_HTTP_TIMEOUT"] == "90"
    assert environment["APT_MIRROR"] == "apt.example"


def test_rauthy_bootstrap_creates_browser_and_device_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LOCAL, "ROOT", tmp_path)
    environment = {
        "RAUTHY_BOOTSTRAP_HOST_DIR": str(tmp_path / ".rauthy-bootstrap"),
        "GATEWAY_HOST_PORT": "8080",
    }

    LOCAL.prepare_rauthy_bootstrap(environment)

    bootstrap = tmp_path / ".rauthy-bootstrap"
    clients = json.loads((bootstrap / "clients.json").read_text())
    browser, device = clients
    assert browser["id"] == "a500-browser-client"
    assert browser["redirect_uris"] == ["http://localhost:8080/"]
    assert browser["default_aud"] == "a500-api"
    assert device["id"] == "a500-device-client"
    assert device["flows_enabled"] == [
        "urn:ietf:params:oauth:grant-type:device_code"
    ]


def test_readiness_uses_configured_issuer_and_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"issuer":"http://localhost:18443/auth/v1/"}'

    def fake_urlopen(url: str, timeout: int) -> Response:
        requested.append(url)
        if url.endswith("/api/v1/signals") or url.endswith(
            "/api/v1/account-snapshot"
        ):
            raise LOCAL.HTTPError(url, 401, "Unauthorized", {}, None)
        return Response()

    monkeypatch.setattr(LOCAL, "urlopen", fake_urlopen)

    LOCAL.wait_ready(
        1,
        gateway_port="18080",
        issuer="http://localhost:18443/auth/v1/",
    )

    assert (
        "http://localhost:18443/auth/v1/.well-known/openid-configuration"
        in requested
    )
    assert "http://localhost:18080" in requested
