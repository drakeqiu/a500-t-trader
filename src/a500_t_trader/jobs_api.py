"""Authenticated HTTP API for persistent historical-data jobs."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .account_snapshot_api import (
    Authenticator,
    OIDCAuthenticator,
    _authenticated_subject,
)
from .backtest import BacktestRepository
from .database import database_url
from .jobs import JobRepository
from .market_data import normalize_symbol


class DailyHistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["daily_history"]
    symbol: str
    start_date: date
    end_date: date


def create_app(
    database: str | None = None,
    authenticator: Authenticator | None = None,
    *,
    persistence_permitted: bool | None = None,
) -> FastAPI:
    database_dsn = database_url(database)
    store = JobRepository(database_dsn)
    daily_history = BacktestRepository(database_dsn)
    auth = authenticator or OIDCAuthenticator.from_environment()
    permitted = (
        persistence_permitted
        if persistence_permitted is not None
        else os.environ.get("HISTORY_PERSISTENCE_PERMITTED", "").lower() == "true"
    )
    app = FastAPI(
        title="A500 jobs API", docs_url=None, redoc_url=None, openapi_url=None
    )

    def respond(value: object, status: int = 200) -> JSONResponse:
        return JSONResponse(
            value, status_code=status, headers={"Cache-Control": "no-store"}
        )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/jobs")
    def list_jobs(
        limit: int = Query(default=30, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        subject = _authenticated_subject(auth, authorization)
        return respond(store.list_for_subject(subject, limit=limit, offset=offset))

    @app.get("/v1/history/daily/symbols")
    def list_daily_history_symbols(
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        _authenticated_subject(auth, authorization)
        return respond(
            {
                "symbols": [
                    {"symbol": symbol, "start_date": start, "end_date": end}
                    for symbol, start, end in (
                        daily_history.list_shared_daily_history_symbols()
                    )
                ]
            }
        )

    @app.get("/v1/history/daily/symbols/{symbol}/ranges")
    def list_daily_history_symbol_ranges(
        symbol: str,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        _authenticated_subject(auth, authorization)
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as exc:
            raise HTTPException(422, "Invalid security symbol") from exc
        return respond(
            {
                "symbol": normalized,
                "ranges": [
                    {"start_date": start, "end_date": end}
                    for start, end in (
                        daily_history.list_shared_daily_history_ranges(normalized)
                    )
                ],
            }
        )

    @app.get("/v1/history/daily")
    def query_daily_history(
        symbol: str,
        start_date: date,
        end_date: date,
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        _authenticated_subject(auth, authorization)
        if start_date > end_date:
            raise HTTPException(422, "start_date must not follow end_date")
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as exc:
            raise HTTPException(422, "Invalid security symbol") from exc
        result = daily_history.get_shared_daily_history_page(
            normalized,
            start_date,
            end_date,
            limit=limit,
            offset=offset,
        )
        if result is None:
            return respond(
                {
                    "symbol": normalized,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "covered": False,
                    "total": 0,
                    "limit": limit,
                    "offset": offset,
                    "has_more": False,
                    "bars": [],
                }
            )
        bars, total = result
        return respond(
            {
                "symbol": normalized,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "covered": True,
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(bars) < total,
                "bars": [
                    {
                        "date": (
                            bar.timestamp.astimezone(ZoneInfo("Asia/Shanghai"))
                            .date()
                            .isoformat()
                        ),
                        "open": str(bar.open),
                        "high": str(bar.high),
                        "low": str(bar.low),
                        "close": str(bar.close),
                        "volume": bar.volume,
                        "amount": None if bar.amount is None else str(bar.amount),
                    }
                    for bar in bars
                ],
            }
        )

    @app.delete("/v1/jobs/{job_id}", status_code=204)
    def delete_job(
        job_id: UUID, authorization: str | None = Header(default=None)
    ) -> Response:
        subject = _authenticated_subject(auth, authorization)
        deleted = store.delete(subject, str(job_id))
        if deleted is None:
            raise HTTPException(404, "Job not found")
        if not deleted:
            raise HTTPException(409, "Only finished jobs can be deleted")
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.post("/v1/jobs")
    def submit(
        request: DailyHistoryRequest, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        subject = _authenticated_subject(auth, authorization)
        if not permitted:
            raise HTTPException(503, "Historical data persistence is not authorized")
        try:
            symbol = normalize_symbol(request.symbol)
        except ValueError as exc:
            raise HTTPException(422, "Invalid security symbol") from exc
        if request.start_date > request.end_date:
            raise HTTPException(422, "start_date must not follow end_date")
        if request.end_date >= datetime.now(ZoneInfo("Asia/Shanghai")).date():
            raise HTTPException(422, "end_date must be before today in Asia/Shanghai")
        job = store.create(subject, symbol, request.start_date, request.end_date)
        return respond(job, 202)

    @app.get("/v1/jobs/{job_id}")
    def get_job(
        job_id: UUID, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        subject = _authenticated_subject(auth, authorization)
        job = store.get(subject, str(job_id))
        if job is None:
            raise HTTPException(404, "Job not found")
        return respond(job)

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel_job(
        job_id: UUID, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        subject = _authenticated_subject(auth, authorization)
        try:
            job = store.cancel(subject, str(job_id))
        except ValueError as exc:
            raise HTTPException(409, "Job cannot be cancelled") from exc
        if job is None:
            raise HTTPException(404, "Job not found")
        return respond(job)

    return app


def main() -> None:
    uvicorn.run(create_app(), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
