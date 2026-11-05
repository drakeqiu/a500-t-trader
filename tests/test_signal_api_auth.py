from __future__ import annotations

from datetime import UTC, datetime

import fakeredis.aioredis
from fastapi.testclient import TestClient

from a500_t_trader.signal_monitor import SignalStore, create_app
from a500_t_trader.strategy.intraday_v1_config import LiveSignalConfig


CONFIG = LiveSignalConfig(("510300",), {}, {}, "stable-fingerprint")


class _TestAuthenticator:
    def __call__(self, token: str) -> str:
        if token not in {"alice-token", "bob-token"}:
            raise ValueError("invalid token")
        return token.removesuffix("-token")


def _state() -> dict[str, object]:
    return {
        "symbol": "510300",
        "bar_timestamp": datetime.now(UTC).isoformat(),
        "strategy_identity": "intraday-etf-t-v1-rsi:stable-fingerprint",
        "rule_version": "V1.0",
        "parameters": {},
        "parameter_fingerprint": CONFIG.fingerprint,
        "status": "BUY",
        "direction": "BUY",
        "execution_eligible": True,
        "reasons": ["shared signal"],
    }


def test_signal_query_and_sse_require_valid_shared_audience_bearer() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = create_app(redis, CONFIG, authenticator=_TestAuthenticator())
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(lambda: SignalStore(redis).publish(_state(), "BUY"))

        for path in ("/v1/signals", "/v1/signals/stream?symbols=510300"):
            assert client.get(path).status_code == 401
            assert (
                client.get(path, headers={"Authorization": "Bearer invalid"}).status_code
                == 401
            )

        alice = {"Authorization": "Bearer alice-token"}
        bob = {"Authorization": "Bearer bob-token"}
        alice_response = client.get("/v1/signals", headers=alice)
        bob_response = client.get("/v1/signals", headers=bob)
        assert alice_response.status_code == bob_response.status_code == 200
        assert alice_response.json() == bob_response.json()
        assert alice_response.json()["signals"][0]["direction"] == "BUY"

        with client.stream(
            "GET", "/v1/signals/stream?symbols=510300", headers=alice
        ) as response:
            lines = iter(response.iter_lines())
            assert response.status_code == 200
            assert next(lines) == "event: snapshot"

        client.portal.call(lambda: redis.aclose())


def test_api_documentation_and_openapi_require_auth_but_health_does_not() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = create_app(redis, CONFIG, authenticator=_TestAuthenticator())
    with TestClient(app) as client:
        assert client.portal is not None
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 401
            assert (
                client.get(path, headers={"Authorization": "Bearer invalid"}).status_code
                == 401
            )

        headers = {"Authorization": "Bearer alice-token"}
        assert client.get("/docs", headers=headers).status_code == 200
        assert client.get("/redoc", headers=headers).status_code == 200
        schema = client.get("/openapi.json", headers=headers)
        assert schema.status_code == 200
        assert "HTTPBearer" in schema.json()["components"]["securitySchemes"]
        assert client.get("/healthz").status_code == 200
        client.portal.call(lambda: redis.aclose())
