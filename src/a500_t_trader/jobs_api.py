"""Authenticated HTTP API for persistent historical-data jobs."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .account_snapshot_api import (
    Authenticator,
    OIDCAuthenticator,
    _authenticated_subject,
)
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
    store = JobRepository(database_url(database))
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

    @app.get("/v1/jobs/{job_id}/results")
    def results(
        job_id: UUID,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        subject = _authenticated_subject(auth, authorization)
        try:
            result = store.results(subject, str(job_id), limit, offset)
        except ValueError as exc:
            raise HTTPException(409, "Job has not succeeded") from exc
        if result is None:
            raise HTTPException(404, "Job not found")
        return respond(result)

    return app


def main() -> None:
    uvicorn.run(create_app(), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
