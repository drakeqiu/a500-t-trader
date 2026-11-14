from __future__ import annotations

import base64
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


def test_rauthy_public_urls_follow_host_port_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(LOCAL, "ROOT", tmp_path)
    monkeypatch.setenv("RAUTHY_HOST_PORT", "18081")
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    monkeypatch.delenv("RAUTHY_PUB_URL", raising=False)
    monkeypatch.delenv("RAUTHY_RP_ORIGIN", raising=False)

    environment = LOCAL.local_environment()

    assert environment["OIDC_ISSUER"] == "http://localhost:18081/auth/v1/"
    assert environment["RAUTHY_PUB_URL"] == "localhost:18081"
    assert environment["RAUTHY_RP_ORIGIN"] == "http://localhost:18081"


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


def test_exported_environment_overrides_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("MARKET_DATA_MODE=tgw\nMAILPIT_HOST_PORT=8010\n")
    monkeypatch.setattr(LOCAL, "ROOT", tmp_path)
    monkeypatch.setenv("MARKET_DATA_MODE", "replay")
    monkeypatch.setenv("MAILPIT_HOST_PORT", "8020")

    environment = LOCAL.local_environment()

    assert environment["MARKET_DATA_MODE"] == "replay"
    assert environment["MAILPIT_HOST_PORT"] == "8020"


def test_local_environment_defaults_mailpit_and_rauthy_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(LOCAL, "ROOT", tmp_path)
    monkeypatch.delenv("MAILPIT_HOST_PORT", raising=False)
    monkeypatch.delenv("RAUTHY_HOST_PORT", raising=False)

    environment = LOCAL.local_environment()

    assert environment["MAILPIT_HOST_PORT"] == "8025"
    assert environment["RAUTHY_HOST_PORT"] == "8081"
    assert environment["RAUTHY_LOCAL_TEST"] == "false"


def test_rauthy_bootstrap_persists_secure_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LOCAL, "ROOT", tmp_path)
    environment = {
        "RAUTHY_BOOTSTRAP_HOST_DIR": str(tmp_path / ".rauthy-bootstrap"),
        "GATEWAY_HOST_PORT": "8080",
    }

    LOCAL.prepare_rauthy_bootstrap(environment)
    first = {
        key: environment[key]
        for key in ("RAUTHY_SECRET_RAFT", "RAUTHY_SECRET_API", "RAUTHY_ENC_KEYS")
    }
    LOCAL.prepare_rauthy_bootstrap(environment)

    assert environment["RAUTHY_NODE_ID"] == "1"
    assert environment["RAUTHY_NODES"] == "1 localhost:8100 localhost:8200"
    assert all(environment[key] == value for key, value in first.items())
    bootstrap = tmp_path / ".rauthy-bootstrap"
    key_id, key = environment["RAUTHY_ENC_KEYS"].split("/", 1)
    assert environment["RAUTHY_ENC_KEY_ACTIVE"] == key_id
    assert len(key_id) == 8
    assert len(base64.b64decode(key, validate=True)) == 32
    assert bootstrap.stat().st_mode & 0o777 == 0o711
    assert all(
        (bootstrap / name).stat().st_mode & 0o777 == 0o600
        for name in (
            "rauthy-secret-raft",
            "rauthy-secret-api",
            "rauthy-enc-key",
            "rauthy-enc-key-id",
            "admin-password",
            "client-update-api-key",
        )
    )
    assert all(
        (bootstrap / name).stat().st_mode & 0o777 == 0o644
        for name in ("clients.json", "users.json", "api_keys.json")
    )


def test_rauthy_bootstrap_api_key_meets_minimum_and_repairs_legacy_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LOCAL, "ROOT", tmp_path)
    bootstrap = tmp_path / "bootstrap"
    environment = {"RAUTHY_BOOTSTRAP_HOST_DIR": str(bootstrap)}

    LOCAL.prepare_rauthy_bootstrap(environment)
    key_path = bootstrap / "client-update-api-key"
    key_name, key_secret = key_path.read_text().strip().split(":", 1)
    assert len(key_secret) >= 64

    key_path.write_text(f"{key_name}:legacy-short-secret\n")
    LOCAL.prepare_rauthy_bootstrap(environment)
    updated_name, updated_secret = key_path.read_text().strip().split(":", 1)
    api_keys = json.loads((bootstrap / "api_keys.json").read_text())

    assert updated_name == key_name
    assert len(updated_secret) >= 64
    assert api_keys[0]["secret"] == {"Plain": updated_secret}


def test_rauthy_bootstrap_sets_client_audience_using_persisted_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LOCAL, "ROOT", tmp_path)
    environment = {"RAUTHY_BOOTSTRAP_HOST_DIR": str(tmp_path / "bootstrap")}
    LOCAL.prepare_rauthy_bootstrap(environment)
    clients = json.loads((tmp_path / "bootstrap" / "clients.json").read_text())
    api_keys = json.loads((tmp_path / "bootstrap" / "api_keys.json").read_text())

    assert all("default_aud" not in client for client in clients)
    assert api_keys[0]["access"] == [
        {"group": "Clients", "access_rights": ["read", "update"]}
    ]
    assert api_keys[0]["secret"]["Plain"]

def test_rauthy_audience_is_updated_and_verified_for_both_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.request import Request

    monkeypatch.setattr(LOCAL, "ROOT", tmp_path)
    environment = {
        "RAUTHY_BOOTSTRAP_HOST_DIR": str(tmp_path / "bootstrap"),
        "OIDC_ISSUER": "http://localhost:8081/auth/v1/",
    }
    LOCAL.prepare_rauthy_bootstrap(environment)
    updates: list[str] = []

    class Response:
        status = 200
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return json.dumps(self.payload).encode()

    class Opener:
        def open(self, request: Request, timeout: int) -> Response:
            if request.get_method() == "PUT":
                updates.append(request.full_url)
                assert json.loads(request.data or b"{}")["default_aud"] == [
                    "a500-api"
                ]
                return Response({})
            if request.data is None:
                return Response({"id": "client", "default_aud": ["a500-api"]})
            return Response({})

    monkeypatch.setattr("urllib.request.build_opener", lambda *handlers: Opener())

    LOCAL.set_client_audience(environment)

    assert updates == [
        "http://localhost:8081/auth/v1/clients/a500-browser-client",
        "http://localhost:8081/auth/v1/clients/a500-device-client",
    ]

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
    assert browser["auth_code_lifetime"] == device["auth_code_lifetime"] == 60
    assert "default_aud" not in browser
    assert "default_aud" not in device
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
