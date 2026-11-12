from __future__ import annotations

import base64
import json
import time

import pytest

from scripts.compose_rauthy_browser_e2e import (
    _email_update_payload,
    _rauthy_settings,
    _validate_discovery,
    internal_jwks_uri,
    verify_access_token,
)


def test_bootstrap_seeds_public_clients_users_and_short_lived_api_key(tmp_path) -> None:
    from scripts.compose_rauthy_browser_e2e import _write_bootstrap

    key_name, secret, password = _write_bootstrap(tmp_path, "http://localhost:18080")
    users = json.loads((tmp_path / "users.json").read_text())
    clients = json.loads((tmp_path / "clients.json").read_text())
    api_keys = json.loads((tmp_path / "api_keys.json").read_text())

    assert {user["email"] for user in users} == {
        "browser@example.test",
        "device@example.test",
        "identity-admin@example.test",
    }
    assert {user["given_name"] for user in users} == {
        "browser",
        "device",
        "identity-admin",
    }
    assert all(user["family_name"] == "Acceptance" for user in users)
    assert tmp_path.stat().st_mode & 0o001
    assert all(path.stat().st_mode & 0o004 for path in tmp_path.iterdir())
    assert all(user["password"] == {"Plain": password} for user in users)
    assert any("admin" in user["roles"] for user in users)
    browser_client = next(
        client for client in clients if client["id"] == "a500-browser-client"
    )
    device_client = next(
        client for client in clients if client["id"] == "a500-device-client"
    )
    assert browser_client["allowed_origins"] == ["http://localhost:18080"]
    assert device_client["allowed_origins"] == []
    assert all(client["secret"] is None for client in clients)
    assert all(client["access_token_alg"] == "RS256" for client in clients)
    assert all(client["id_token_alg"] == "RS256" for client in clients)
    assert all(
        10 <= client["auth_code_lifetime"] <= 300
        for client in clients
        if "authorization_code" in client["flows_enabled"]
    )
    assert key_name == "a500-acceptance"
    assert api_keys[0]["secret"] == {"Plain": secret}
    assert len(secret) == 64 and secret.isalnum()
    assert 1700 <= api_keys[0]["exp"] - int(time.time()) <= 1800
    assert api_keys[0]["access"] == [
        {"group": "Clients", "access_rights": ["read", "create", "update"]},
        {"group": "Users", "access_rights": ["read", "update"]},
    ]
    assert all(user["password"] == {"Plain": password} for user in users)


def token(header: dict[str, object], claims: dict[str, object]) -> str:
    def encode(value: dict[str, object]) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
        )

    return f"{encode(header)}.{encode(claims)}.signature"


def test_accepts_required_access_token_contract() -> None:
    now = int(time.time())
    issuer = "https://localhost:18443/auth/v1/"
    claims = {
        "iss": issuer,
        "aud": ["a500-api"],
        "iat": now,
        "exp": now + 300,
    }
    verify_access_token(token({"alg": "RS256"}, claims), issuer, now=now)


@pytest.mark.parametrize(
    "header,claims",
    [
        (
            {"alg": "EdDSA"},
            {
                "iss": "https://localhost:18443/auth/v1/",
                "aud": "a500-api",
                "iat": 100,
                "exp": 400,
            },
        ),
        (
            {"alg": "RS256"},
            {"iss": "https://wrong", "aud": "a500-api", "iat": 100, "exp": 400},
        ),
        (
            {"alg": "RS256"},
            {
                "iss": "https://localhost:18443/auth/v1/",
                "aud": "other",
                "iat": 100,
                "exp": 400,
            },
        ),
        (
            {"alg": "RS256"},
            {
                "iss": "https://localhost:18443/auth/v1/",
                "aud": "a500-api",
                "iat": 100,
                "exp": 401,
            },
        ),
    ],
)
def test_rejects_wrong_token_contract(
    header: dict[str, object], claims: dict[str, object]
) -> None:
    issuer = "https://localhost:18443/auth/v1/"
    with pytest.raises(AssertionError):
        verify_access_token(token(header, claims), issuer, now=200)


def test_internal_jwks_keeps_compose_route_and_query() -> None:
    jwks = "https://localhost:18443/auth/v1/oidc/certs?k=v"
    assert internal_jwks_uri(jwks) == ("http://rauthy:8080/auth/v1/oidc/certs?k=v")


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
    _validate_discovery(
        expected_issuer,
        {
            "issuer": expected_issuer,
            "jwks_uri": "https://localhost:18443/auth/v1/oidc/certs",
        },
    )


def test_rejects_discovery_with_mismatched_issuer() -> None:
    with pytest.raises(RuntimeError, match="issuer/JWKS mismatch"):
        _validate_discovery(
            "https://localhost:18443/auth/v1/",
            {
                "issuer": "https://wrong:18443/auth/v1/",
                "jwks_uri": "https://localhost:18443/auth/v1/oidc/certs",
            },
        )


def test_wait_discovery_returns_validated_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import compose_rauthy_browser_e2e as runner

    issuer = "https://localhost:18443/auth/v1/"
    discovery = {
        "issuer": issuer,
        "jwks_uri": issuer + "oidc/certs",
        "token_endpoint": issuer + "oidc/token",
    }

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def read(self) -> bytes:
            return json.dumps(discovery).encode()

    monkeypatch.setattr(runner, "urlopen", lambda *args, **kwargs: Response())
    ticks = iter((0, 0, 181))
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    assert runner._wait_discovery(issuer) == discovery


def test_email_update_preserves_the_user_record_and_changes_only_email() -> None:
    current_user = {
        "id": "stable-subject",
        "email": "before@example.test",
        "enabled": True,
        "roles": ["customer"],
    }

    updated_user = _email_update_payload(current_user, "after@example.test")

    assert updated_user == {
        "id": "stable-subject",
        "email": "after@example.test",
        "enabled": True,
        "roles": ["customer"],
    }
    assert current_user["email"] == "before@example.test"


def test_extract_recovery_link_from_mailpit_message() -> None:
    from scripts.compose_rauthy_browser_e2e import _recovery_link

    assert _recovery_link(
        [{"Text": "Use this link: https://localhost:18443/auth/v1/reset-password?token=secret"}],
        "localhost",
    ) == "https://localhost:18443/auth/v1/reset-password?token=secret"


def test_recovery_link_rejects_non_localhost_and_missing_link() -> None:
    from scripts.compose_rauthy_browser_e2e import _recovery_link

    with pytest.raises(AssertionError, match="local Rauthy"):
        _recovery_link([{"Text": "https://example.com/reset?token=secret"}], "localhost")
    with pytest.raises(AssertionError, match="no recovery link"):
        _recovery_link([{"Text": "https://localhost/account"}], "localhost")
def test_refresh_token_accepts_local_self_signed_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import compose_rauthy_browser_e2e as runner

    class Response:
        status = 401

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            pass

    def fake_urlopen(*args: object, **kwargs: object) -> Response:
        context = kwargs["context"]
        assert isinstance(context, runner.ssl.SSLContext)
        assert context.verify_mode == runner.ssl.CERT_NONE
        return Response()

    monkeypatch.setattr(runner, "urlopen", fake_urlopen)

    assert runner._refresh_token("https://localhost/token", "client", "secret") == 401


def test_extract_recovery_link_from_mailpit_html() -> None:
    from scripts.compose_rauthy_browser_e2e import _recovery_link

    assert _recovery_link(
        [{"HTML": '<a href="https://localhost/auth/reset?token=secret&amp;code=once">Reset</a>'}],
        "localhost",
    ) == "https://localhost/auth/reset?token=secret&code=once"
