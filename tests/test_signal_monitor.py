from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient as FastAPITestClient

import a500_t_trader.signal_monitor as signal_monitor
from a500_t_trader.market_data import MinuteBar
from a500_t_trader.signal_calculator import SignalCalculator
from a500_t_trader.signal_monitor import SignalStore
from a500_t_trader.signal_monitor import create_app as create_signal_app
from a500_t_trader.strategy.intraday_v1_config import LiveSignalConfig


class _TestAuthenticator:
    def __call__(self, token: str) -> str:
        if token != "test-token":
            raise ValueError("invalid token")
        return "test-subject"


class AuthenticatedTestClient(FastAPITestClient):
    def __init__(self, app: FastAPI) -> None:
        super().__init__(app, headers={"Authorization": "Bearer test-token"})


def create_app(
    redis: fakeredis.aioredis.FakeRedis,
    config: LiveSignalConfig,
) -> FastAPI:
    return create_signal_app(redis, config, authenticator=_TestAuthenticator())


def test_signal_api_default_app_imports_without_connecting_to_redis() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "os.environ['A500_OIDC_ISSUER']='https://issuer.example'; "
                "os.environ['A500_OIDC_AUDIENCE']='a500-api'; "
                "os.environ['A500_OIDC_JWKS_URI']='https://issuer.example/jwks'; "
                "from a500_t_trader.signal_api import app; "
                "assert app.title == 'A500 signal monitor'"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


CONFIG = LiveSignalConfig(("510300", "563360"), {}, {}, "stable-fingerprint")


def state(symbol: str, status: str, timestamp: datetime) -> dict[str, object]:
    return {
        "symbol": symbol,
        "bar_timestamp": timestamp.isoformat(),
        "strategy_identity": "intraday-etf-t-v1-rsi:stable-fingerprint",
        "rule_version": "V1.0",
        "parameters": {},
        "parameter_fingerprint": "stable-fingerprint",
        "status": status,
        "direction": (
            status if status in {"BUY", "SELL", "BUYBACK", "TAKE_PROFIT"} else None
        ),
        "execution_eligible": True,
        "reasons": ["test market condition"],
    }


def test_http_status_configured_symbols_and_selection() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with AuthenticatedTestClient(create_app(redis, CONFIG)) as client:
        assert client.portal is not None
        all_result = client.get("/v1/signals")
        assert all_result.status_code == 200
        assert [item["symbol"] for item in all_result.json()["signals"]] == [
            "510300",
            "563360",
        ]

        filtered = client.get("/v1/signals", params={"symbols": "563360"})
        assert filtered.status_code == 200
        assert [item["symbol"] for item in filtered.json()["signals"]] == ["563360"]
        client.portal.call(lambda: redis.aclose())


def test_http_rejects_unconfigured_symbols_and_reports_strategy_metadata() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with AuthenticatedTestClient(create_app(redis, CONFIG)) as client:
        assert client.portal is not None
        response = client.get("/v1/signals", params={"symbols": "510999"})
        assert response.status_code == 422
        unavailable = client.get("/v1/signals", params={"symbols": "510300"})
        signal = unavailable.json()["signals"][0]
        assert signal["strategy_identity"] == "intraday-etf-t-v1-rsi:stable-fingerprint"
        assert signal["parameter_fingerprint"] == CONFIG.fingerprint
        assert signal["status"] == "UNAVAILABLE"
        client.portal.call(lambda: redis.aclose())


def test_stream_requires_explicit_symbols() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with AuthenticatedTestClient(create_app(redis, CONFIG)) as client:
        assert client.portal is not None
        assert client.get("/v1/signals/stream").status_code == 422
        client.portal.call(lambda: redis.aclose())


def test_http_preserves_fresh_execution_eligibility_as_reference_data() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with AuthenticatedTestClient(create_app(redis, CONFIG)) as client:
        assert client.portal is not None
        current = state("510300", "BUY", datetime.now(UTC))
        client.portal.call(lambda: SignalStore(redis).publish(current, "BUY"))
        response = client.get("/v1/signals", params={"symbols": "510300"})
        signal = response.json()["signals"][0]
        assert signal["direction"] == "BUY"
        assert signal["execution_eligible"] is True
        client.portal.call(lambda: redis.aclose())


def test_http_status_marks_stale_signal_unavailable_without_direction() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    timestamp = datetime.now(UTC) - timedelta(minutes=30)
    with AuthenticatedTestClient(create_app(redis, CONFIG)) as client:
        assert client.portal is not None
        client.portal.call(
            lambda: SignalStore(redis).publish(state("510300", "BUY", timestamp), "BUY")
        )
        response = client.get("/v1/signals", params={"symbols": "510300"})
        signal = response.json()["signals"][0]
        assert response.status_code == 200
        assert signal["status"] == "UNAVAILABLE"
        assert signal["direction"] is None
        assert signal["quantity"] is None
        client.portal.call(lambda: redis.aclose())
        assert signal["execution_eligible"] is False


def test_directional_history_replays_in_order_while_wait_status_is_coalesced() -> None:
    async def scenario() -> tuple[
        list[tuple[str, dict[str, object]]],
        dict[str, object] | None,
        dict[str, object] | None,
    ]:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SignalStore(redis)
        now = datetime.now(UTC)
        first = await store.publish(state("510300", "BUY", now), "BUY")
        await store.publish(state("510300", "WAIT", now + timedelta(minutes=1)), "WAIT")
        second = await store.publish(
            state("510300", "SELL", now + timedelta(minutes=2)), "SELL"
        )
        replay = await store.replay(first or "")
        current = await store.get("510300")
        restored = await SignalStore(redis).get("510300")
        assert second is not None
        await redis.aclose()
        return replay, current, restored

    replay, current, restored = asyncio.run(scenario())
    assert [row[1]["status"] for row in replay] == ["SELL"]
    assert current is not None and current["status"] == "SELL"
    assert restored is not None and restored["status"] == "SELL"


def test_directional_history_retains_only_the_24_hour_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[bool, list[tuple[str, dict[str, object]]]]:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SignalStore(redis)
        now = datetime.now(UTC)
        first = await store.publish(state("510300", "BUY", now), "BUY")
        await asyncio.sleep(0.01)
        second = await store.publish(
            state("510300", "SELL", now + timedelta(minutes=1)),
            "SELL",
        )
        assert first is not None and second is not None
        try:
            await store.replay(first)
        except ValueError:
            first_expired = True
        else:
            first_expired = False
        latest = await store.replay(second)
        await redis.aclose()
        return first_expired, latest

    assert signal_monitor.RETENTION == timedelta(hours=24)
    monkeypatch.setattr(signal_monitor, "RETENTION", timedelta(milliseconds=1))
    first_expired, latest = asyncio.run(scenario())
    assert first_expired
    assert latest == []


def test_health_reports_redis_unavailability() -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    with AuthenticatedTestClient(create_app(redis, CONFIG)) as client:
        assert client.portal is not None
        assert client.get("/healthz").status_code == 503
        client.portal.call(lambda: redis.aclose())


def test_signal_query_reports_redis_unavailability_without_signal_data() -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    with AuthenticatedTestClient(create_app(redis, CONFIG)) as client:
        assert client.portal is not None
        response = client.get("/v1/signals")
        assert response.status_code == 503
        assert response.json() == {"detail": "signal store unavailable"}
        client.portal.call(lambda: redis.aclose())


def test_sse_sends_snapshot_replays_after_cursor_and_resets_expired_cursor() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with AuthenticatedTestClient(create_app(redis, CONFIG)) as client:
        assert client.portal is not None
        now = datetime.now(UTC)
        store = SignalStore(redis)
        first = client.portal.call(
            lambda: store.publish(state("510300", "BUY", now), "BUY")
        )
        client.portal.call(
            lambda: store.publish(
                state("510300", "SELL", now + timedelta(minutes=1)), "SELL"
            )
        )

        with client.stream("GET", "/v1/signals/stream?symbols=510300") as response:
            lines = iter(response.iter_lines())
            assert response.status_code == 200
            assert next(lines) == "event: snapshot"
        with client.stream(
            "GET",
            "/v1/signals/stream?symbols=510300",
            headers={"Last-Event-ID": first or ""},
        ) as response:
            lines = iter(response.iter_lines())
            assert next(lines).startswith("id: ")
            assert next(lines) == "event: sell"
        with client.stream(
            "GET",
            "/v1/signals/stream?symbols=510300",
            headers={"Last-Event-ID": "0-0"},
        ) as response:
            lines = iter(response.iter_lines())
            assert next(lines) == "event: reset"
            assert next(lines) == 'data: {"reason":"cursor unavailable"}'
            assert next(lines) == ""
            assert next(lines) == "event: snapshot"
        client.portal.call(lambda: redis.aclose())


def test_sse_filters_delivery_without_changing_global_event_order() -> None:
    config = LiveSignalConfig(
        ("510300", "563360", "000001"), {}, {}, "stable-fingerprint"
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with AuthenticatedTestClient(create_app(redis, config)) as client:
        assert client.portal is not None
        now = datetime.now(UTC)
        store = SignalStore(redis)
        first = client.portal.call(
            lambda: store.publish(state("510300", "BUY", now), "BUY")
        )
        client.portal.call(
            lambda: store.publish(
                state("000001", "SELL", now + timedelta(minutes=1)), "SELL"
            )
        )
        second = client.portal.call(
            lambda: store.publish(
                state("563360", "SELL", now + timedelta(minutes=2)), "SELL"
            )
        )
        third = client.portal.call(
            lambda: store.publish(
                state("510300", "BUYBACK", now + timedelta(minutes=3)), "BUYBACK"
            )
        )

        with client.stream(
            "GET", "/v1/signals/stream?symbols=563360&symbols=510300"
        ) as response:
            lines = iter(response.iter_lines())
            assert next(lines) == "event: snapshot"
            snapshots = set()
            snapshots.add(json.loads(next(lines).removeprefix("data: "))["symbol"])
            assert next(lines) == ""
            assert next(lines) == "event: snapshot"
            snapshots.add(json.loads(next(lines).removeprefix("data: "))["symbol"])
            assert snapshots == {"563360", "510300"}

        with client.stream(
            "GET",
            "/v1/signals/stream?symbols=563360&symbols=510300",
            headers={"Last-Event-ID": first or ""},
        ) as response:
            lines = iter(response.iter_lines())
            assert next(lines) == f"id: {second}"
            assert next(lines) == "event: sell"
            assert '"symbol":"563360"' in next(lines)
            assert next(lines) == ""
            assert next(lines) == f"id: {third}"
            assert next(lines) == "event: buyback"
            assert '"symbol":"510300"' in next(lines)
        client.portal.call(lambda: redis.aclose())


def test_calculator_consumes_duplicate_market_event_once() -> None:
    server = fakeredis.FakeServer()
    redis = fakeredis.FakeRedis(server=server, decode_responses=True)
    api_redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    now = datetime.now(UTC)
    calls: list[str] = []

    def evaluate(
        symbol: str,
        bar: MinuteBar,
        history: list[MinuteBar],
        config: LiveSignalConfig,
        as_of: datetime,
    ) -> dict[str, object]:
        calls.append(bar.timestamp.isoformat())
        return state(symbol, "BUY", bar.timestamp)

    calculator = SignalCalculator(redis, CONFIG, evaluate)
    event = {
        "event_type": "minute_bar",
        "symbol": "510300",
        "data": {
            "symbol": "510300",
            "timestamp": now.isoformat(),
            "interval_minutes": 5,
            "open": "1.0",
            "high": "1.1",
            "low": "0.9",
            "close": "1.05",
            "volume": 100,
            "amount": "100.0",
            "is_completed": True,
            "source": "replay",
        },
    }
    calculator.handle(event)
    calculator.handle(event)

    assert len(calls) == 1
    with AuthenticatedTestClient(create_app(api_redis, CONFIG)) as client:
        assert client.portal is not None
        response = client.get("/v1/signals", params={"symbols": "510300"})
        assert response.status_code == 200
        assert response.json()["signals"][0]["status"] == "BUY"
        assert response.json()["signals"][0]["schema_version"] == 1
        client.portal.call(lambda: api_redis.aclose())
    redis.close()
