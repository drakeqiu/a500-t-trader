"""Deterministic HTTP mocks used by the frontend Playwright suite.

The fixture provides a local OIDC provider and canned, in-memory API responses.
It never starts application API services or connects to PostgreSQL, Redis, or market
data. Run only through Playwright, or: uv run python
scripts/frontend-api-mock.py --test-mode --port 38100.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import html
import json
import os
import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlencode

import uvicorn
from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)


def create_fixture(port: int) -> FastAPI:
    tokens: dict[str, str] = {}
    subject_tokens: dict[str, str] = {}
    codes: dict[str, dict[str, str]] = {}
    snapshots: dict[str, tuple[dict[str, object], int]] = {}
    batches: dict[str, dict[str, object]] = {}
    jobs: dict[str, dict[str, object]] = {}
    imported_subjects: set[str] = set()
    signal_states: dict[str, dict[str, object]] = {}
    signal_events: list[tuple[str, dict[str, object]]] = []
    signal_subscribers: set[asyncio.Queue[tuple[str, dict[str, object]] | None]] = set()
    signal_event_sequence = 0
    stream_generation = 0
    issuer = f"http://127.0.0.1:{port}/fixture-idp"
    app = FastAPI(title="Frontend API mock fixture")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[f"http://127.0.0.1:{os.getenv('A500_E2E_WEB_PORT', '3100')}"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    def authenticate(token: str) -> str:
        if token not in tokens:
            raise ValueError("Unknown fixture bearer token")
        return tokens[token]

    def require_subject(authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                401,
                "Bearer credentials required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            return authenticate(authorization[7:])
        except ValueError as error:
            raise HTTPException(
                401,
                "Invalid bearer credentials",
                headers={"WWW-Authenticate": "Bearer"},
            ) from error

    def make_signal_state(status: str, symbol: str = "563360") -> dict[str, object]:
        return {
            "schema_version": 1,
            "symbol": symbol,
            "bar_timestamp": datetime.now(UTC).isoformat(),
            "strategy_identity": "intraday-etf-t-v1-rsi:browser-fixture",
            "rule_version": "V1.0",
            "parameters": {},
            "parameter_fingerprint": "browser-fixture",
            "status": status,
            "direction": status if status in {"BUY", "SELL"} else None,
            "quantity": 100 if status in {"BUY", "SELL"} else None,
            "execution_eligible": False,
            "reasons": ["deterministic API mock; not live"],
            "stale": False,
        }

    def send_signal(state: dict[str, object]) -> str:
        nonlocal signal_event_sequence
        signal_event_sequence += 1
        event_id = f"{signal_event_sequence}-0"
        signal_events.append((event_id, state))
        for queue in tuple(signal_subscribers):
            queue.put_nowait((event_id, state))
        return event_id

    def sse(event: str, data: object, event_id: str | None = None) -> str:
        fields = []
        if event_id is not None:
            fields.append(f"id: {event_id}")
        fields.extend((f"event: {event}", f"data: {json.dumps(data)}"))
        return "\n".join(fields) + "\n\n"

    @app.middleware("http")
    async def fixture_transport_fault(request: Request, call_next):
        generation = stream_generation
        response = await call_next(request)
        if request.url.path != "/v1/signals/stream":
            return response
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
        subject = authorization["subject"]
        access_token = subject_tokens.setdefault(subject, secrets.token_urlsafe(32))
        tokens[access_token] = subject
        return {"access_token": access_token, "token_type": "Bearer", "expires_in": 300}

    @app.get("/v1/signals")
    async def get_signals(
        symbols: Annotated[list[str] | None, Query()] = None,
        authorization: str | None = Header(default=None),
    ):
        require_subject(authorization)
        selected = symbols or ["510300", "563360"]
        return {
            "signals": [
                signal_states.get(symbol, make_signal_state("UNAVAILABLE", symbol))
                for symbol in selected
            ],
            "strategy": "intraday-etf-t-v1-rsi",
            "parameter_fingerprint": "browser-fixture",
        }

    @app.get("/v1/signals/stream")
    async def stream_signals(
        request: Request,
        symbols: Annotated[list[str], Query()],
        last_event_id: str | None = None,
        authorization: str | None = Header(default=None),
    ):
        require_subject(authorization)
        generation = stream_generation
        cursor = last_event_id or request.headers.get("last-event-id")
        queue: asyncio.Queue[tuple[str, dict[str, object]] | None] = asyncio.Queue()
        signal_subscribers.add(queue)

        async def events():
            try:
                replay = []
                if cursor:
                    ids = [event_id for event_id, _ in signal_events]
                    if cursor in ids:
                        replay = [
                            (event_id, state)
                            for event_id, state in signal_events[
                                ids.index(cursor) + 1 :
                            ]
                        ]
                    else:
                        yield sse("reset", {"reason": "cursor unavailable"})
                        for symbol in symbols:
                            state = signal_states.get(
                                symbol, make_signal_state("UNAVAILABLE", symbol)
                            )
                            yield sse("snapshot", state)
                else:
                    for symbol in symbols:
                        state = signal_states.get(
                            symbol, make_signal_state("UNAVAILABLE", symbol)
                        )
                        yield sse("snapshot", state)
                for event_id, state in replay:
                    if state["symbol"] in symbols:
                        yield sse(str(state["status"]).lower(), state, event_id)
                while (
                    not await request.is_disconnected()
                    and generation == stream_generation
                ):
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=0.1)
                        if event is None:
                            break
                        event_id, state = event
                    except TimeoutError:
                        continue
                    if state["symbol"] in symbols:
                        yield sse(str(state["status"]).lower(), state, event_id)
            finally:
                signal_subscribers.discard(queue)

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.put("/v1/account-snapshot")
    async def put_snapshot(
        request: Request,
        authorization: str | None = Header(default=None),
        if_match: str | None = Header(default=None, alias="If-Match"),
        if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    ):
        subject = require_subject(authorization)
        existing = snapshots.get(subject)
        if existing is None:
            if if_none_match != "*" or if_match is not None:
                raise HTTPException(412, "Account snapshot does not exist")
            version = 1
            status = 201
        else:
            _, version = existing
            if if_match != f'"snapshot-{version}"':
                raise HTTPException(412, "ETag does not match")
            version += 1
            status = 200
        snapshot = await request.json()
        saved = {
            **snapshot,
            "updated_at": datetime.now(UTC).isoformat(),
            "classification": "user-entered; not broker-verified",
        }
        snapshots[subject] = (saved, version)
        return JSONResponse(
            saved, status_code=status, headers={"ETag": f'"snapshot-{version}"'}
        )

    @app.get("/v1/account-snapshot")
    async def get_snapshot(authorization: str | None = Header(default=None)):
        subject = require_subject(authorization)
        saved = snapshots.get(subject)
        if saved is None:
            raise HTTPException(404, "Account snapshot not found")
        snapshot, version = saved
        return JSONResponse(snapshot, headers={"ETag": f'"snapshot-{version}"'})

    @app.get("/v1/account-nav")
    async def get_nav(authorization: str | None = Header(default=None)):
        subject = require_subject(authorization)
        if subject not in snapshots:
            return JSONResponse(
                {
                    "status": "unavailable",
                    "nav": None,
                    "reason": "account_snapshot_missing",
                },
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        return {
            "status": "unavailable",
            "nav": None,
            "reason": "market_data_unavailable",
        }

    @app.post("/v1/trade-imports/previews")
    async def create_trade_preview(
        file: Annotated[UploadFile, File()],
        authorization: str | None = Header(default=None),
    ):
        subject = require_subject(authorization)
        content = (await file.read()).decode("gb18030", errors="replace")
        invalid = ",0,1.264," in content
        batch_id = str(uuid.uuid4())
        preview = {
            "batch_id": batch_id,
            "valid": 1,
            "new": 0 if invalid or subject in imported_subjects else 1,
            "duplicates": 0 if invalid or subject not in imported_subjects else 1,
            "errors": 1 if invalid else 0,
            "error_details": (
                [{"row": 3, "field": "成交数量", "reason": "must be positive"}]
                if invalid
                else []
            ),
            "confirmed": None,
        }
        batches[(subject, batch_id)] = preview
        return {key: value for key, value in preview.items() if key != "confirmed"}

    @app.get("/v1/trade-imports/previews/{batch_id}")
    async def get_trade_preview(
        batch_id: str, authorization: str | None = Header(default=None)
    ):
        subject = require_subject(authorization)
        preview = batches.get((subject, batch_id))
        if preview is None:
            raise HTTPException(404, "Preview not found")
        return preview

    @app.post("/v1/trade-imports/previews/{batch_id}/confirm")
    async def confirm_trade_preview(
        batch_id: str, authorization: str | None = Header(default=None)
    ):
        subject = require_subject(authorization)
        preview = batches.get((subject, batch_id))
        if preview is None:
            raise HTTPException(404, "Preview not found")
        if preview["errors"]:
            raise HTTPException(409, "Preview contains validation errors")
        if preview["confirmed"] is None:
            preview["confirmed"] = {
                "valid": preview["valid"],
                "new": preview["new"],
                "duplicates": preview["duplicates"],
                "errors": 0,
                "inserted_ids": [1],
                "confirmed": True,
            }
            imported_subjects.add(subject)
        return preview["confirmed"]

    @app.post("/v1/jobs")
    async def create_job(
        request: Request, authorization: str | None = Header(default=None)
    ):
        require_subject(authorization)
        body = await request.json()
        job_id = str(uuid.uuid4())
        job = {
            "id": job_id,
            "type": body["type"],
            "symbol": body["symbol"],
            "start_date": body["start_date"],
            "end_date": body["end_date"],
            "state": "queued",
            "progress": 0,
            "created_at": datetime.now(UTC).isoformat(),
        }
        jobs[job_id] = job
        return JSONResponse(job, status_code=202)

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str, authorization: str | None = Header(default=None)):
        require_subject(authorization)
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    @app.post("/v1/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, authorization: str | None = Header(default=None)):
        require_subject(authorization)
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        job["state"] = "cancelled"
        return job

    @app.get("/v1/jobs/{job_id}/results")
    async def get_job_results(
        job_id: str,
        limit: int = Query(default=100),
        offset: int = Query(default=0),
        authorization: str | None = Header(default=None),
    ):
        require_subject(authorization)
        if job_id not in jobs:
            raise HTTPException(404, "Job not found")
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    @app.post("/__fixture/signals/{status}")
    async def publish(status: str):
        if status not in {"WAIT", "BUY", "SELL", "UNAVAILABLE"}:
            raise HTTPException(422, "Unsupported fixture state")
        state = make_signal_state(status)
        signal_states["563360"] = state
        return {"event_id": send_signal(state)}

    @app.post("/__fixture/disconnect-streams")
    async def disconnect_streams():
        nonlocal stream_generation
        stream_generation += 1
        for queue in tuple(signal_subscribers):
            queue.put_nowait(None)
        return {"generation": stream_generation}

    @app.post("/__fixture/expire-events")
    async def expire_events():
        signal_events.clear()
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
