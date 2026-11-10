from __future__ import annotations

import base64
import json
import time

import pytest

from scripts.compose_rauthy_browser_e2e import internal_jwks_uri, verify_access_token


def token(header: dict[str, object], claims: dict[str, object]) -> str:
    def encode(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return f"{encode(header)}.{encode(claims)}.signature"


def test_accepts_required_access_token_contract() -> None:
    now = int(time.time())
    verify_access_token(token({"alg": "RS256"}, {"iss": "https://localhost:18443", "aud": ["a500-api"], "iat": now, "exp": now + 300}), "https://localhost:18443", now=now)


@pytest.mark.parametrize("header,claims", [
    ({"alg": "EdDSA"}, {"iss": "https://localhost:18443", "aud": "a500-api", "iat": 100, "exp": 400}),
    ({"alg": "RS256"}, {"iss": "https://wrong", "aud": "a500-api", "iat": 100, "exp": 400}),
    ({"alg": "RS256"}, {"iss": "https://localhost:18443", "aud": "other", "iat": 100, "exp": 400}),
    ({"alg": "RS256"}, {"iss": "https://localhost:18443", "aud": "a500-api", "iat": 100, "exp": 401}),
])
def test_rejects_wrong_token_contract(header: dict[str, object], claims: dict[str, object]) -> None:
    with pytest.raises(AssertionError):
        verify_access_token(token(header, claims), "https://localhost:18443", now=200)


def test_internal_jwks_keeps_compose_route_and_query() -> None:
    assert internal_jwks_uri("https://localhost:18443/auth/v1/oidc/jwks?k=v") == "http://rauthy:8080/auth/v1/oidc/jwks?k=v"


def test_rejects_remote_or_http_jwks() -> None:
    with pytest.raises(ValueError):
        internal_jwks_uri("https://idp.example.test/keys")
    with pytest.raises(ValueError):
        internal_jwks_uri("http://localhost:18443/keys")
