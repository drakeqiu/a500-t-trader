from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import a500_t_trader.account_snapshot_api as account_api
from a500_t_trader.account_snapshot_api import OIDCAuthenticator, create_app

ISSUER = "https://identity.example.test"
SHARED_AUDIENCE = "a500-api"
LEGACY_AUDIENCE = "a500-account-api"


class StaticJwks:
    def __init__(self, jwks_uri: str, key: Any) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, token: str) -> Any:
        return type("SigningKey", (), {"key": self.key})()


def _token(
    private_key: Any,
    *,
    subject: str = "alice",
    issuer: str = ISSUER,
    audience: str = SHARED_AUDIENCE,
    expires_at: datetime | None = None,
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": subject,
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": expires_at or now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


@pytest.fixture
def signing_keys() -> tuple[Any, Any]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: Any,
) -> TestClient:
    monkeypatch.setattr(
        account_api,
        "PyJWKClient",
        lambda jwks_uri: StaticJwks(jwks_uri, key),
    )
    authenticator = OIDCAuthenticator(ISSUER, SHARED_AUDIENCE, "https://jwks.test")
    return TestClient(create_app(tmp_path / "accounts.sqlite3", authenticator))


def test_account_api_accepts_signed_shared_audience_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: tuple[Any, Any],
) -> None:
    private_key, public_key = signing_keys
    with _client(tmp_path, monkeypatch, public_key) as client:
        response = client.get(
            "/v1/account-snapshot",
            headers={"Authorization": f"Bearer {_token(private_key)}"},
        )
    assert response.status_code == 404


def test_account_snapshot_ownership_uses_verified_subject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: tuple[Any, Any],
) -> None:
    private_key, public_key = signing_keys
    alice = {"Authorization": f"Bearer {_token(private_key, subject='alice')}"}
    bob = {"Authorization": f"Bearer {_token(private_key, subject='bob')}"}
    snapshot = {
        "as_of": "2026-11-04T09:30:00+08:00",
        "holdings": {"510300": 100},
        "protected_floor": {"510300": 20},
        "sellable": {"510300": 80},
        "cost_basis": {"510300": "3.8500"},
        "available_cash": "125.50",
    }
    with _client(tmp_path, monkeypatch, public_key) as client:
        created = client.put(
            "/v1/account-snapshot",
            json=snapshot,
            headers={**alice, "If-None-Match": "*"},
        )
        assert created.status_code == 201
        assert client.get(
            "/v1/account-snapshot", headers=bob
        ).status_code == 404
        alice_snapshot = client.get("/v1/account-snapshot", headers=alice).json()
        assert alice_snapshot["holdings"] == {"510300": 100}


@pytest.mark.parametrize(
    "claims",
    [
        {"issuer": "https://untrusted.example.test/"},
        {"audience": LEGACY_AUDIENCE},
        {"expires_at": datetime.now(UTC) - timedelta(minutes=5)},
    ],
    ids=["untrusted-issuer", "legacy-audience", "expired"],
)
def test_account_api_rejects_invalid_oidc_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: tuple[Any, Any],
    claims: dict[str, Any],
) -> None:
    private_key, public_key = signing_keys
    with _client(tmp_path, monkeypatch, public_key) as client:
        response = client.get(
            "/v1/account-snapshot",
            headers={"Authorization": f"Bearer {_token(private_key, **claims)}"},
        )
    assert response.status_code == 401


def test_account_api_rejects_invalid_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: tuple[Any, Any],
) -> None:
    _, trusted_public_key = signing_keys
    untrusted_private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    )
    with _client(tmp_path, monkeypatch, trusted_public_key) as client:
        response = client.get(
            "/v1/account-snapshot",
            headers={
                "Authorization": f"Bearer {_token(untrusted_private_key)}"
            },
        )
    assert response.status_code == 401


def test_account_api_requires_bearer_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: tuple[Any, Any],
) -> None:
    _, public_key = signing_keys
    with _client(tmp_path, monkeypatch, public_key) as client:
        response = client.get("/v1/account-snapshot")
    assert response.status_code == 401


def test_account_api_requires_complete_oidc_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "A500_OIDC_ISSUER",
        "A500_OIDC_AUDIENCE",
        "A500_OIDC_JWKS_URI",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="must be configured"):
        OIDCAuthenticator.from_environment()


def test_account_api_does_not_configure_legacy_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A500_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("A500_OIDC_AUDIENCE", LEGACY_AUDIENCE)
    monkeypatch.setenv("A500_OIDC_JWKS_URI", "https://jwks.test")
    with pytest.raises(ValueError, match="shared audience"):
        OIDCAuthenticator.from_environment()


def test_account_api_uses_shared_audience_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A500_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("A500_OIDC_AUDIENCE", SHARED_AUDIENCE)
    monkeypatch.setenv("A500_OIDC_JWKS_URI", "https://jwks.test")
    assert OIDCAuthenticator.from_environment().audience == SHARED_AUDIENCE
