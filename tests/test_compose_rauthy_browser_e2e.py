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
)


def token(header: dict[str, object], claims: dict[str, object]) -> str:
    def encode(value: dict[str, object]) -> str:
        encoded = json.dumps(value).encode()
        return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode()

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
    with pytest.raises(AssertionError):
        verify_access_token(
            token(header, claims), "https://localhost:18443/auth/v1/", now=200
        )


def test_internal_jwks_keeps_compose_route_and_query() -> None:
    assert (
        internal_jwks_uri("https://localhost:18443/auth/v1/oidc/certs?k=v")
        == "http://rauthy:8080/auth/v1/oidc/certs?k=v"
    )


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
        "RAUTHY_LISTEN_SCHEME": "https",
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


def test_market_data_image_tags_are_project_scoped() -> None:
    images = _market_data_images("a500-rauthy-abc123")
    assert images == {
        "MARKET_DATA_CONSUMER_IMAGE": "a500-rauthy-abc123-market-data-consumer:local",
        "MARKET_DATA_REPLAY_IMAGE": "a500-rauthy-abc123-market-data-replay:local",
    }
