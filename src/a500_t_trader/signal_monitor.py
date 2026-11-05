"""Shared Redis-backed signal state, ordered event history, and async HTTP/SSE API."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis

from .account_snapshot_api import Authenticator, OIDCAuthenticator
from .strategy.intraday_v1_config import (
    STRATEGY_ID,
    LiveSignalConfig,
    load_live_signal_config,
)

RETENTION = timedelta(hours=24)
STREAM_KEY = "a500:signals:events"
UPDATES_CHANNEL = "a500:signals:updates"


class SignalStore:
    """Redis contract shared by the producer and API, independent of process memory."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def health(self) -> bool:
        return bool(await self.redis.ping())

    async def get(self, symbol: str) -> dict[str, Any] | None:
        value = await self.redis.get(f"a500:signals:state:{symbol}")
        if value is None:
            return None
        return cast(dict[str, Any], json.loads(value))

    async def publish(self, state: dict[str, Any], event_type: str) -> str | None:
        """Persist latest state; retain only distinct directional events in stream."""
        symbol = str(state["symbol"])
        pipe = self.redis.pipeline(transaction=True)
        pipe.set(
            f"a500:signals:state:{symbol}",
            json.dumps(state, ensure_ascii=False, default=str),
        )
        directional = event_type in {"BUY", "SELL", "BUYBACK", "TAKE_PROFIT"}
        if directional:
            pipe.xadd(
                STREAM_KEY,
                {
                    "symbol": symbol,
                    "type": event_type,
                    "data": json.dumps(state, ensure_ascii=False, default=str),
                },
            )
            cutoff = int((datetime.now(UTC) - RETENTION).timestamp() * 1000)
            pipe.xtrim(STREAM_KEY, minid=f"{cutoff}-0", approximate=False)
        results = await pipe.execute()
        if directional:
            event_id = results[1]
            return event_id.decode() if isinstance(event_id, bytes) else str(event_id)
        await self.redis.publish(
            UPDATES_CHANNEL,
            json.dumps(state, ensure_ascii=False, default=str),
        )
        return None

    async def replay(self, cursor: str) -> list[tuple[str, dict[str, Any]]]:
        await self.trim()
        rows = await self.redis.xrange(STREAM_KEY, min="-", max="+")
        if not rows:
            raise ValueError("cursor unavailable")
        first = _text(rows[0][0])
        last = _text(rows[-1][0])
        if (
            not _valid_cursor(cursor)
            or _id_key(cursor) < _id_key(first)
            or _id_key(cursor) > _id_key(last)
        ):
            raise ValueError("cursor unavailable")
        results: list[tuple[str, dict[str, Any]]] = []
        rows = await self.redis.xrange(
            STREAM_KEY,
            min=f"({cursor}",
            max="+",
        )
        for event_id, fields in rows or []:
            payload = (fields or {}).get(b"data", (fields or {}).get("data"))
            results.append((_text(event_id), json.loads(_text(payload))))
        return results

    async def after(self, cursor: str) -> list[tuple[str, dict[str, Any]]]:
        await self.trim()
        rows = await self.redis.xrange(STREAM_KEY, min=f"({cursor}", max="+")
        return [
            (
                _text(event_id),
                json.loads(
                    _text((fields or {}).get(b"data", (fields or {}).get("data")))
                ),
            )
            for event_id, fields in rows or []
        ]

    async def latest_id(self) -> str:
        await self.trim()
        rows = await self.redis.xrevrange(STREAM_KEY, max="+", min="-", count=1)
        return "0-0" if not rows else _text(rows[0][0])

    async def trim(self) -> None:
        cutoff = int((datetime.now(UTC) - RETENTION).timestamp() * 1000)
        await self.redis.xtrim(STREAM_KEY, minid=f"{cutoff}-0", approximate=False)


def _id_key(value: str) -> tuple[int, int]:
    first, second = value.split("-", maxsplit=1)
    return int(first), int(second)


def create_app(
    redis: Redis | None = None,
    config: LiveSignalConfig | None = None,
    *,
    freshness: timedelta = timedelta(minutes=15),
    authenticator: Authenticator | None = None,
) -> FastAPI:
    """Construct read-only web service; it never connects to a market/broker source."""
    if redis is None:
        redis = Redis.from_url(
            os.environ.get("A500_REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
    if config is None:
        config = load_live_signal_config()
    auth = (
        authenticator
        if authenticator is not None
        else OIDCAuthenticator.from_environment()
    )
    bearer = HTTPBearer(auto_error=False)
    app = FastAPI(
        title="A500 signal monitor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = config
    store = SignalStore(redis)
    app.state.signal_store = store
    app.state.redis = redis
    from .web_oidc import create_web_router

    app.include_router(create_web_router())

    async def require_auth(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="Bearer credentials required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            auth(credentials.credentials)
        except Exception as error:
            raise HTTPException(
                status_code=401,
                detail="Invalid bearer credentials",
                headers={"WWW-Authenticate": "Bearer"},
            ) from error

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        try:
            await store.health()
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="signal store unavailable"
            ) from error
        return {"status": "ok"}

    @app.get(
        "/openapi.json",
        include_in_schema=False,
        dependencies=[Depends(require_auth)],
    )
    async def openapi_schema() -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/docs", include_in_schema=False, dependencies=[Depends(require_auth)])
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title=f"{app.title} - Swagger UI",
        )

    @app.get("/redoc", include_in_schema=False, dependencies=[Depends(require_auth)])
    async def redoc() -> HTMLResponse:
        return get_redoc_html(
            openapi_url="/openapi.json",
            title=f"{app.title} - ReDoc",
        )

    @app.get("/v1/signals", dependencies=[Depends(require_auth)])
    async def signals(
        symbols: list[str] | None = Query(default=None),  # noqa: B008
    ) -> dict[str, Any]:
        selected = config.etfs if symbols is None else tuple(dict.fromkeys(symbols))
        _validate_symbols(selected, config)
        try:
            states = [
                await _current(store, symbol, freshness, config) for symbol in selected
            ]
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="signal store unavailable"
            ) from error
        return {
            "signals": states,
            "strategy": "intraday-etf-t-v1-rsi",
            "parameter_fingerprint": config.fingerprint,
        }

    @app.get("/v1/signals/stream", dependencies=[Depends(require_auth)])
    async def stream(
        request: Request,
        symbols: list[str] = Query(...),  # noqa: B008
        last_event_id: str | None = None,
    ) -> StreamingResponse:
        selected = tuple(dict.fromkeys(symbols))
        _validate_symbols(selected, config)
        cursor = last_event_id or request.headers.get("last-event-id")
        return StreamingResponse(
            _events(store, selected, cursor, freshness, config, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _unavailable_state(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "status": "UNAVAILABLE",
        "direction": None,
        "quantity": None,
        "execution_eligible": False,
        "stale": True,
        "reasons": ["signal store unavailable"],
    }


async def _current(
    store: SignalStore,
    symbol: str,
    freshness: timedelta,
    config: LiveSignalConfig,
) -> dict[str, Any]:
    state = await store.get(symbol)
    if state is None:
        return {
            "schema_version": 1,
            "symbol": symbol,
            "bar_timestamp": None,
            "strategy_identity": f"{STRATEGY_ID}:{config.fingerprint}",
            "rule_version": "V1.0",
            "parameters": config.parameters,
            "parameter_fingerprint": config.fingerprint,
            "status": "UNAVAILABLE",
            "direction": None,
            "quantity": None,
            "execution_eligible": False,
            "reasons": ["no signal state available"],
            "stale": True,
        }
    state.setdefault("schema_version", 1)
    try:
        timestamp = datetime.fromisoformat(str(state["bar_timestamp"]))
        age = datetime.now(UTC) - timestamp.astimezone(UTC)
        if (
            timestamp.tzinfo is None
            or timestamp.utcoffset() is None
            or age < timedelta(0)
            or age > freshness
        ):
            raise ValueError
    except KeyError, ValueError, TypeError:
        return {
            **state,
            "status": "UNAVAILABLE",
            "direction": None,
            "quantity": None,
            "stale": True,
            "execution_eligible": False,
            "reasons": [
                *list(state.get("reasons") or []),
                "signal state is stale or has invalid event time",
            ],
        }
    return {**state, "stale": False}


async def _events(
    store: SignalStore,
    symbols: tuple[str, ...],
    cursor: str | None,
    freshness: timedelta,
    config: LiveSignalConfig,
    request: Request,
) -> AsyncIterator[str]:
    connection = store.redis.pubsub()
    pubsub: PubSub | None = connection
    try:
        await connection.subscribe(UPDATES_CHANNEL)
    except Exception:
        await connection.aclose()  # type: ignore[no-untyped-call]
        pubsub = None
        yield _sse(
            "unavailable",
            {"status": "UNAVAILABLE", "reasons": ["signal store unavailable"]},
        )

    last_status: dict[str, dict[str, Any]] = {}
    reset_required = False
    next_id = "0-0"
    if cursor is not None:
        try:
            replay = await store.replay(cursor)
        except Exception:
            reset_required = True
            yield _sse("reset", {"reason": "cursor unavailable"})
            try:
                next_id = await store.latest_id()
            except Exception:
                next_id = "0-0"
        else:
            for event_id, state in replay:
                if state.get("symbol") in symbols:
                    yield _sse(
                        str(state.get("status", "signal")).lower(), state, event_id
                    )
            next_id = replay[-1][0] if replay else cursor
    if cursor is None or reset_required:
        try:
            next_id = await store.latest_id()
        except Exception:
            next_id = "0-0"
        for symbol in symbols:
            try:
                snapshot = await _current(store, symbol, freshness, config)
            except Exception:
                snapshot = _unavailable_state(symbol)
            last_status[symbol] = snapshot
            yield _sse("snapshot", snapshot)
    last_status_check = 0.0
    try:
        while not await request.is_disconnected():
            received_update = False
            if pubsub is not None:
                try:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=0,
                    )
                except Exception:
                    message = None
                if message is not None and message.get("type") == "message":
                    update = json.loads(_text(message["data"]))
                    symbol = update.get("symbol")
                    if symbol in symbols:
                        try:
                            state = await _current(
                                store, str(symbol), freshness, config
                            )
                        except Exception:
                            state = _unavailable_state(str(symbol))
                        received_update = True
                        if state != last_status.get(str(symbol)):
                            last_status[str(symbol)] = state
                            yield _sse(
                                str(state.get("status", "unavailable")).lower(), state
                            )

            now = time.monotonic()
            if now - last_status_check >= 1.0:
                last_status_check = now
                for symbol in symbols:
                    try:
                        state = await _current(store, symbol, freshness, config)
                    except Exception:
                        state = _unavailable_state(symbol)
                    previous = last_status.get(symbol)
                    last_status[symbol] = state
                    if previous is not None and state != previous:
                        received_update = True
                        yield _sse(
                            str(state.get("status", "unavailable")).lower(), state
                        )
            try:
                events = await store.after(next_id)
            except Exception:
                yield _sse(
                    "unavailable",
                    {"status": "UNAVAILABLE", "reasons": ["signal store unavailable"]},
                )
                await asyncio.sleep(1)
                continue
            for event_id, state in events:
                next_id = event_id
                if state.get("symbol") in symbols:
                    yield _sse(
                        str(state.get("status", "signal")).lower(), state, event_id
                    )
            if not events and not received_update:
                await asyncio.sleep(0.5)
    finally:
        if pubsub is not None:
            await pubsub.aclose()  # type: ignore[no-untyped-call]


def _sse(event: str, data: dict[str, Any], event_id: str | None = None) -> str:
    identifier = "" if event_id is None else f"id: {event_id}\n"
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"{identifier}event: {event}\ndata: {payload}\n\n"


def _validate_symbols(symbols: Iterable[str], config: LiveSignalConfig) -> None:
    invalid = sorted(set(symbols) - set(config.etfs))
    if invalid:
        raise HTTPException(status_code=422, detail={"unconfigured_symbols": invalid})


def _valid_cursor(value: str) -> bool:
    parts = value.split("-")
    return len(parts) == 2 and all(part.isdigit() for part in parts)


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
