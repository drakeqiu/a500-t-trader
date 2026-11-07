"""Explicit browser-test fixtures around the real account and signal HTTP routes.

Not a live IdP or market feed: authorization code flow and Redis are deterministic
fixtures. PostgreSQL test data and tokens exist only for this process's lifetime.
Run only through Playwright, or: uv run python scripts/frontend-integration-fixture.py
--test-mode --port 38100. There is no production configuration fallback.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import html
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import fakeredis.aioredis
import psycopg
import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from psycopg import sql

from a500_t_trader.account_snapshot_api import create_app as create_account_app
from a500_t_trader.signal_monitor import STREAM_KEY
from a500_t_trader.signal_monitor import create_app as create_signal_app
from a500_t_trader.strategy.intraday_v1_config import LiveSignalConfig


def create_fixture(port: int) -> FastAPI:
    base_url = os.environ.get("A500_TEST_DATABASE_URL")
    if not base_url:
        raise RuntimeError(
            "A500_TEST_DATABASE_URL must point to a PostgreSQL test database"
        )
    parts = urlsplit(base_url)
    if parts.scheme not in ("postgres", "postgresql"):
        raise ValueError("A500_TEST_DATABASE_URL must be a PostgreSQL URL")
    schema = "a500_browser_" + uuid.uuid4().hex
    params = parse_qsl(parts.query, keep_blank_values=True)
    options = [value for key, value in params if key == "options"]
    params = [(key, value) for key, value in params if key != "options"]
    params.append(("options", " ".join([*options, f"-csearch_path={schema}"])))
    database_url = urlunsplit(parts._replace(query=urlencode(params)))
    with psycopg.connect(base_url, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def drop_schema() -> None:
        with psycopg.connect(base_url, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    tokens: dict[str, str] = {}
    codes: dict[str, dict[str, str]] = {}
    stream_generation = 0
    issuer = f"http://127.0.0.1:{port}/fixture-idp"
    config = LiveSignalConfig(("510300", "563360"), {}, {}, "browser-fixture")

    def authenticate(token: str) -> str:
        if token not in tokens:
            raise ValueError("Unknown fixture bearer token")
        return tokens[token]

    try:
        account = create_account_app(database_url, authenticate, market_data=None)
        signals = create_signal_app(redis, config, authenticator=authenticate)
        store = signals.state.signal_store
    except BaseException:
        drop_schema()
        raise

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            try:
                await redis.aclose()
            finally:
                drop_schema()

    app = FastAPI(title="Explicit A500 browser-test fixture", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[f"http://127.0.0.1:{os.getenv('A500_E2E_WEB_PORT', '3100')}"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def fixture_transport_fault(request: Request, call_next):
        generation = stream_generation
        response = await call_next(request)
        if request.url.path != "/v1/signals/stream":
            return response
        # Fault only the transport, retaining the real API's SSE bytes and IDs.
        original = response.body_iterator

        async def disconnectable():
            pending = asyncio.create_task(anext(original))
            try:
                while generation == stream_generation:
                    done, _ = await asyncio.wait({pending}, timeout=0.05)
                    if not done:
                        continue
                    try:
                        chunk = pending.result()
                    except StopAsyncIteration:
                        break
                    yield chunk
                    pending = asyncio.create_task(anext(original))
            finally:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                    await pending
                await original.aclose()

        response.body_iterator = disconnectable()
        return response

    app.include_router(account.router)
    app.include_router(signals.router)

    @app.get("/__fixture/health")
    async def health():
        return {"mode": "deterministic-browser-test", "live_idp": False}

    @app.get("/fixture-idp/.well-known/openid-configuration")
    async def discovery():
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
        }

    def validate_authorization(
        client_id: str,
        redirect_uri: str,
        response_type: str,
        scope: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> None:
        expected_redirect = (
            f"http://127.0.0.1:{os.getenv('A500_E2E_WEB_PORT', '3100')}/"
        )
        if (
            client_id != "a500-browser-client"
            or redirect_uri != expected_redirect
            or response_type != "code"
            or "openid" not in scope.split()
            or not state
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge)
            or code_challenge_method != "S256"
        ):
            raise HTTPException(400, "Invalid fixture authorization request")

    @app.get("/fixture-idp/authorize", response_class=HTMLResponse)
    async def authorize(
        client_id: str,
        redirect_uri: str,
        response_type: str,
        scope: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
    ):
        values = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": response_type,
            "scope": scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
        }
        validate_authorization(**values)
        hidden = "".join(
            f"<input type='hidden' name='{name}' "
            f"value='{html.escape(value, quote=True)}'>"
            for name, value in values.items()
        )
        return (
            "<!doctype html><html lang='en'><title>Fixture authorization</title>"
            "<h1>Deterministic test IdP — not Keycloak</h1>"
            f"<form method='post'>{hidden}"
            "<label>Fixture account<input name='subject' required></label>"
            "<button type='submit'>Authorize fixture account</button></form></html>"
        )

    @app.post("/fixture-idp/authorize")
    async def approve(
        client_id: str = Form(),
        redirect_uri: str = Form(),
        response_type: str = Form(),
        scope: str = Form(),
        state: str = Form(),
        code_challenge: str = Form(),
        code_challenge_method: str = Form(),
        subject: str = Form(),
    ):
        validate_authorization(
            client_id,
            redirect_uri,
            response_type,
            scope,
            state,
            code_challenge,
            code_challenge_method,
        )
        if not subject.strip():
            raise HTTPException(400, "Empty fixture subject")
        code = secrets.token_urlsafe(32)
        codes[code] = {
            "subject": subject.strip(),
            "challenge": code_challenge,
            "redirect_uri": redirect_uri,
        }
        return RedirectResponse(
            f"{redirect_uri}?{urlencode({'code': code, 'state': state})}",
            status_code=303,
        )

    @app.post("/fixture-idp/token")
    async def token(
        code: str = Form(),
        grant_type: str = Form(),
        client_id: str = Form(),
        redirect_uri: str = Form(),
        code_verifier: str = Form(),
    ):
        if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", code_verifier):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        authorization = codes.pop(code, None)
        challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode("ascii")).digest()
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        if (
            client_id != "a500-browser-client"
            or grant_type != "authorization_code"
            or authorization is None
            or redirect_uri != authorization["redirect_uri"]
            or not secrets.compare_digest(challenge, authorization["challenge"])
        ):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access_token = secrets.token_urlsafe(32)
        tokens[access_token] = authorization["subject"]
        return {"access_token": access_token, "token_type": "Bearer", "expires_in": 300}

    @app.post("/__fixture/signals/{status}")
    async def publish(status: str):
        if status not in {"WAIT", "BUY", "SELL", "UNAVAILABLE"}:
            raise HTTPException(422, "Unsupported fixture state")
        event_id = await store.publish(
            {
                "schema_version": 1,
                "symbol": "563360",
                "bar_timestamp": datetime.now(UTC).isoformat(),
                "strategy_identity": "intraday-etf-t-v1-rsi:browser-fixture",
                "rule_version": "V1.0",
                "parameters": {},
                "parameter_fingerprint": "browser-fixture",
                "status": status,
                "direction": status if status in {"BUY", "SELL"} else None,
                "quantity": 100 if status in {"BUY", "SELL"} else None,
                "execution_eligible": False,
                "reasons": ["deterministic browser-test market fixture; not live"],
            },
            status,
        )
        return {"event_id": event_id}

    @app.post("/__fixture/disconnect-streams")
    async def disconnect_streams():
        nonlocal stream_generation
        stream_generation += 1
        return {"generation": stream_generation}

    @app.post("/__fixture/expire-events")
    async def expire_events():
        await redis.delete(STREAM_KEY)
        return {"expired": True}

    @app.get("/__fixture/csv/{kind}")
    async def csv(kind: str):
        if kind not in {"valid", "invalid"}:
            raise HTTPException(404, "Unknown fixture CSV")
        header = (
            "成交日期,成交时间,证券代码,证券名称,操作,成交数量,成交均价,成交金额,"
            "合同编号,成交编号,手续费,印花税,其他杂费,发生金额,资金余额,交易市场,"
            "股东帐户,交收日期,证券中文全称,\n"
        )
        row = (
            "20261030,10:01:02,563360,A500基金,买入,8000,1.264,10112,"
            "contract-1001,1001,0.510,0.000,0.000,-10112.510,"
            "PRIVATE-BALANCE,沪Ａ,PRIVATE-ACCOUNT,20261030,A500ETF华泰柏瑞,\n"
        )
        content = header + row
        if kind == "invalid":
            content += row.replace(",8000,1.264,", ",0,1.264,").replace(
                ",1001,", ",1002,"
            )
        return Response(content.encode("gb18030"), media_type="text/csv")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-mode", action="store_true", required=True)
    parser.add_argument("--port", type=int, default=38100)
    args = parser.parse_args()
    uvicorn.run(create_fixture(args.port), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
