from __future__ import annotations

import base64
import json
import time

import pytest

from scripts.compose_rauthy_browser_e2e import (
    _market_data_images,
    _rauthy_settings,
    _validate_discovery,
    internal_jwks_uri,
    verify_access_token,
    _write_bootstrap,
)


def token(header: dict[str, object], claims: dict[str, object]) -> str:
    def encode(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return f"{encode(header)}.{encode(claims)}.signature"


def test_accepts_required_access_token_contract() -> None:
    now = int(time.time())
    issuer = "https://localhost:18443/auth/v1/"
    verify_access_token(token({"alg": "RS256"}, {"iss": issuer, "aud": ["a500-api"], "iat": now, "exp": now + 300}), issuer, now=now)


@pytest.mark.parametrize("header,claims", [
    ({"alg": "EdDSA"}, {"iss": "https://localhost:18443/auth/v1/", "aud": "a500-api", "iat": 100, "exp": 400}),
    ({"alg": "RS256"}, {"iss": "https://wrong", "aud": "a500-api", "iat": 100, "exp": 400}),
    ({"alg": "RS256"}, {"iss": "https://localhost:18443/auth/v1/", "aud": "other", "iat": 100, "exp": 400}),
    ({"alg": "RS256"}, {"iss": "https://localhost:18443/auth/v1/", "aud": "a500-api", "iat": 100, "exp": 401}),
])
def test_rejects_wrong_token_contract(header: dict[str, object], claims: dict[str, object]) -> None:
    with pytest.raises(AssertionError):
        verify_access_token(token(header, claims), "https://localhost:18443/auth/v1/", now=200)


def test_internal_jwks_keeps_compose_route_and_query() -> None:
    assert internal_jwks_uri("https://localhost:18443/auth/v1/oidc/certs?k=v") == "http://rauthy:8080/auth/v1/oidc/certs?k=v"


def test_rejects_remote_or_http_jwks() -> None:
    with pytest.raises(ValueError):
        internal_jwks_uri("https://idp.example.test/keys")
    with pytest.raises(ValueError):
        internal_jwks_uri("http://localhost:18443/keys")


def test_rauthy_public_issuer_settings_match_discovery() -> None:
    settings = _rauthy_settings("18443")
    expected_issuer = "https://localhost:18443/auth/v1/"
    assert settings == {
        "OIDC_ISSUER": expected_issuer,
        "RAUTHY_HOST_PORT": "18443",
        "RAUTHY_LISTEN_SCHEME": "http_https",
        "RAUTHY_PUB_URL": "localhost:18443",
    }
    _validate_discovery(expected_issuer, {
        "issuer": expected_issuer,
        "jwks_uri": "https://localhost:18443/auth/v1/oidc/certs",
    })


def test_rejects_discovery_with_mismatched_issuer() -> None:
    with pytest.raises(RuntimeError, match="issuer/JWKS mismatch"):
        _validate_discovery("https://localhost:18443/auth/v1/", {
            "issuer": "https://wrong:18443/auth/v1/",
            "jwks_uri": "https://localhost:18443/auth/v1/oidc/certs",
        })


def test_market_data_image_tags_are_project_scoped() -> None:
    images = _market_data_images("a500-rauthy-abc123")
    assert images == {
        "MARKET_DATA_CONSUMER_IMAGE": "a500-rauthy-abc123-market-data-consumer:local",
        "MARKET_DATA_REPLAY_IMAGE": "a500-rauthy-abc123-market-data-replay:local",
    }


def test_bootstrap_clients_use_supported_audience_update_path(tmp_path) -> None:
    bootstrap = tmp_path / "bootstrap"
    secret = "x" * 64
    _write_bootstrap(bootstrap, "https://gateway.example.test", "user@example.test", "test-password", secret)
    clients = json.loads((bootstrap / "clients.json").read_text())
    api_keys = json.loads((bootstrap / "api_keys.json").read_text())
    users = json.loads((bootstrap / "users.json").read_text())
    assert users[0]["given_name"] == "Acceptance"
    assert users[0]["family_name"] == "Tester"
    assert users[0]["email_verified"] is True
    assert [client["id"] for client in clients] == ["a500-browser-client", "a500-device-client"]
    assert [client["flows_enabled"] for client in clients] == [
        ["authorization_code", "refresh_token"],
        ["urn:ietf:params:oauth:grant-type:device_code"],
    ]
    assert all("default_aud" not in client for client in clients)
    assert api_keys[0]["name"] == "acceptance-client-update"
    assert api_keys[0]["exp"] >= int(time.time())
    assert api_keys[0]["secret"] == {"Plain": secret}
    assert len(api_keys[0]["secret"]["Plain"]) >= 64
    assert api_keys[0]["access"] == [{"group": "Clients", "access_rights": ["update"]}]
    for client in clients:
        assert client["access_token_alg"] == "RS256"
        assert client["id_token_alg"] == "RS256"
        assert client["access_token_lifetime"] == 300


def test_bootstrap_key_is_created_after_wheel_and_image_builds(tmp_path, monkeypatch) -> None:
    from scripts import compose_rauthy_browser_e2e as runner

    events: list[str] = []
    bootstrap = tmp_path / "bootstrap"

    def run(command, **_kwargs):
        if command[0] == "uv":
            events.append("wheel")
        elif "build" in command:
            events.append("images")
        else:
            events.append("start")
            key = json.loads((bootstrap / "api_keys.json").read_text())[0]
            assert key["exp"] > int(time.time())

    monkeypatch.setattr(runner.subprocess, "run", run)
    credentials = runner._build_bootstrap_and_start_rauthy(
        ["podman", "compose"], {}, bootstrap, "http://localhost:8765"
    )
    assert credentials[0].startswith("acceptance-")
    assert events == ["wheel", "images", "start"]
