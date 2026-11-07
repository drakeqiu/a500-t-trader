"""Durable, subject-scoped daily-history jobs and their independent worker."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .cli.runtime import (
    RecoverableNetworkError,
    _error_category,
    create_provider_from_env,
)
from .database import database_url
from .market_data import SHANGHAI, DailyBar, normalize_symbol

_LOG = logging.getLogger(__name__)
_LEASE_SECONDS = 120
_HEARTBEAT_SECONDS = 30
_MAX_ATTEMPTS = 3


class DailyHistoryProvider(Protocol):
    def get_daily_history(
        self, symbol: str, start_date: date, end_date: date
    ) -> tuple[DailyBar, ...]: ...


def _date(value: date | str) -> date:
    if isinstance(value, datetime):
        raise ValueError("expected a date, not a datetime")
    return value if isinstance(value, date) else date.fromisoformat(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime, uuid.UUID)):
        return value.isoformat() if not isinstance(value, uuid.UUID) else str(value)
    return value


def _job(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "type": "daily_history",
        **{key: _json_value(value) for key, value in row.items()},
    }


_COLUMNS = (
    "id, symbol, start_date, end_date, status, stage, cancel_requested, total_chunks, "
    "completed_chunks, created_at, updated_at, finished_at, error"
)


class JobRepository:
    """PostgreSQL task store; all public reads and mutations are subject-scoped."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        with self._connect() as db:
            # Serialize concurrent startup migrations, including first deployment.
            db.execute("SELECT pg_advisory_xact_lock(500004)")
            db.execute("""CREATE TABLE IF NOT EXISTS daily_history_jobs (
                id UUID PRIMARY KEY, subject TEXT NOT NULL, symbol TEXT NOT NULL,
                start_date DATE NOT NULL, end_date DATE NOT NULL, status TEXT NOT NULL,
                total_chunks INTEGER NOT NULL,
                completed_chunks INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ,
                error TEXT, lease_token UUID, lease_until TIMESTAMPTZ,
                cancel_requested BOOLEAN NOT NULL DEFAULT false
            )""")
            db.execute("""ALTER TABLE daily_history_jobs
                ADD COLUMN IF NOT EXISTS stage TEXT NOT NULL DEFAULT 'queued'""")
            db.execute("""CREATE INDEX IF NOT EXISTS daily_history_jobs_claim_idx
                ON daily_history_jobs (status, lease_until, created_at)""")
            db.execute("""CREATE TABLE IF NOT EXISTS daily_history_job_bars (
                job_id UUID NOT NULL REFERENCES daily_history_jobs(id)
                    ON DELETE CASCADE,
                bar_date DATE NOT NULL, bar_json JSONB NOT NULL,
                PRIMARY KEY (job_id, bar_date)
            )""")
            # Separate from the CLI cache: its legacy ranges have no acquisition time.
            db.execute("""CREATE TABLE IF NOT EXISTS daily_history_job_cache (
                symbol TEXT NOT NULL, start_date DATE NOT NULL, end_date DATE NOT NULL,
                fetched_at TIMESTAMPTZ NOT NULL, bars JSONB NOT NULL,
                PRIMARY KEY (symbol, start_date, end_date)
            )""")

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def create(
        self, subject: str, symbol: str, start_date: date | str, end_date: date | str
    ) -> dict[str, Any]:
        start, end = _date(start_date), _date(end_date)
        if not subject:
            raise ValueError("subject is required")
        if start > end:
            raise ValueError("start_date must be on or before end_date")
        if end >= datetime.now(SHANGHAI).date():
            raise ValueError("end_date must be before today in Asia/Shanghai")
        symbol = normalize_symbol(symbol)
        with self._connect() as db:
            row = db.execute(
                f"INSERT INTO daily_history_jobs (id, subject, symbol, start_date, "
                f"end_date, status, total_chunks) VALUES (%s,%s,%s,%s,%s,'queued',%s) "
                f"RETURNING {_COLUMNS}",
                (uuid.uuid4(), subject, symbol, start, end, end.year - start.year + 1),
            ).fetchone()
        assert row is not None
        return _job(row)  # type: ignore[return-value]

    def get(self, subject: str, job_id: str) -> dict[str, Any] | None:
        try:
            identifier = uuid.UUID(str(job_id))
        except ValueError, TypeError, AttributeError:
            return None
        with self._connect() as db:
            row = db.execute(
                f"SELECT {_COLUMNS} FROM daily_history_jobs WHERE subject=%s AND id=%s",
                (subject, identifier),
            ).fetchone()
        return _job(row)

    def cancel(self, subject: str, job_id: str) -> dict[str, Any] | None:
        try:
            identifier = uuid.UUID(str(job_id))
        except ValueError, TypeError, AttributeError:
            return None
        with self._connect() as db:
            row = db.execute(
                f"""UPDATE daily_history_jobs SET
                    cancel_requested = CASE WHEN status IN ('queued','running')
                        THEN true ELSE cancel_requested END,
                    status = CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
                    stage = CASE WHEN status='queued' THEN 'finished' ELSE stage END,
                    finished_at = CASE WHEN status='queued' THEN now()
                        ELSE finished_at END,
                    updated_at = CASE WHEN status IN ('queued','running')
                        THEN now() ELSE updated_at END
                    WHERE subject=%s AND id=%s RETURNING {_COLUMNS}""",
                (subject, identifier),
            ).fetchone()
        return _job(row)

    def results(
        self, subject: str, job_id: str, limit: int, offset: int
    ) -> dict[str, Any] | None:
        if limit <= 0 or offset < 0:
            raise ValueError("limit must be positive and offset non-negative")
        try:
            identifier = uuid.UUID(str(job_id))
        except ValueError, TypeError, AttributeError:
            return None
        with self._connect() as db:
            job = db.execute(
                "SELECT status FROM daily_history_jobs "
                "WHERE subject=%s AND id=%s FOR SHARE",
                (subject, identifier),
            ).fetchone()
            if job is None:
                return None
            if job["status"] != "succeeded":
                raise ValueError("job has not succeeded")
            total_row = db.execute(
                "SELECT count(*) AS total FROM daily_history_job_bars WHERE job_id=%s",
                (identifier,),
            ).fetchone()
            rows = db.execute(
                "SELECT bar_json FROM daily_history_job_bars WHERE job_id=%s "
                "ORDER BY bar_date LIMIT %s OFFSET %s",
                (identifier, limit, offset),
            ).fetchall()
        assert total_row is not None
        return {
            "job_id": str(identifier),
            "total": total_row["total"],
            "limit": limit,
            "offset": offset,
            "bars": [row["bar_json"] for row in rows],
        }

    def claim(self) -> dict[str, Any] | None:
        token = uuid.uuid4()
        with self._connect() as db:
            # A cancelled task with a dead worker is finalized on recovery.
            db.execute("""UPDATE daily_history_jobs SET status='cancelled',
                stage='finished', finished_at=now(), updated_at=now(),
                lease_token=NULL, lease_until=NULL
                WHERE status='running' AND cancel_requested AND lease_until < now()""")
            row = db.execute(
                """WITH candidate AS (
                SELECT id FROM daily_history_jobs
                WHERE status='queued' OR (status='running' AND lease_until < now()
                    AND NOT cancel_requested)
                ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1
            ) UPDATE daily_history_jobs j SET status='running', stage='checking_cache',
                lease_token=%s, lease_until=now() + (%s * interval '1 second'),
                updated_at=now()
                FROM candidate WHERE j.id=candidate.id
                RETURNING j.id, j.symbol, j.start_date, j.end_date,
                    j.completed_chunks, j.total_chunks, j.lease_token""",
                (token, _LEASE_SECONDS),
            ).fetchone()
        return row

    def heartbeat(self, job_id: uuid.UUID, token: uuid.UUID) -> bool:
        with self._connect() as db:
            row = db.execute(
                """UPDATE daily_history_jobs SET
                lease_until=now() + (%s * interval '1 second')
                WHERE id=%s AND status='running' AND lease_token=%s
                    AND lease_until > now() AND NOT cancel_requested RETURNING id""",
                (_LEASE_SECONDS, job_id, token),
            ).fetchone()
        return row is not None

    def set_stage(self, job_id: uuid.UUID, token: uuid.UUID, stage: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                """UPDATE daily_history_jobs SET stage=%s, updated_at=now()
                WHERE id=%s AND status='running' AND lease_token=%s
                    AND lease_until > now() AND NOT cancel_requested RETURNING id""",
                (stage, job_id, token),
            ).fetchone()
        return row is not None

    def cancelled_or_lost(self, job_id: uuid.UUID, token: uuid.UUID) -> bool:
        with self._connect() as db:
            row = db.execute(
                """SELECT cancel_requested FROM daily_history_jobs
                WHERE id=%s AND status='running' AND lease_token=%s
                    AND lease_until > now()""",
                (job_id, token),
            ).fetchone()
        return row is None or bool(row["cancel_requested"])

    def cached(
        self, symbol: str, start: date, end: date
    ) -> list[dict[str, Any]] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT bars FROM daily_history_job_cache
                WHERE symbol=%s AND start_date=%s AND end_date=%s
                    AND fetched_at >= ((end_date + 1)::timestamp
                        AT TIME ZONE 'Asia/Shanghai')""",
                (symbol, start, end),
            ).fetchone()
        return None if row is None else row["bars"]

    def checkpoint(
        self,
        job_id: uuid.UUID,
        token: uuid.UUID,
        symbol: str,
        start: date,
        end: date,
        bars: list[dict[str, Any]],
        fetched: bool,
    ) -> bool:
        with self._connect() as db:
            row = db.execute(
                """SELECT cancel_requested FROM daily_history_jobs
                WHERE id=%s AND status='running' AND lease_token=%s
                    AND lease_until > now() FOR UPDATE""",
                (job_id, token),
            ).fetchone()
            if row is None or row["cancel_requested"]:
                return False
            if fetched:
                db.execute(
                    """INSERT INTO daily_history_job_cache
                    (symbol,start_date,end_date,fetched_at,bars)
                    VALUES (%s,%s,%s,now(),%s)
                    ON CONFLICT (symbol,start_date,end_date) DO UPDATE
                    SET fetched_at=excluded.fetched_at, bars=excluded.bars""",
                    (symbol, start, end, Jsonb(bars)),
                )
            for bar in bars:
                db.execute(
                    """INSERT INTO daily_history_job_bars (job_id,bar_date,bar_json)
                    VALUES (%s,%s,%s)""",
                    (job_id, date.fromisoformat(bar["timestamp"][:10]), Jsonb(bar)),
                )
            db.execute(
                """UPDATE daily_history_jobs SET completed_chunks=completed_chunks+1,
                updated_at=now() WHERE id=%s""",
                (job_id,),
            )
        return True

    def finish(
        self, job_id: uuid.UUID, token: uuid.UUID, error: str | None = None
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE daily_history_jobs SET
                status=CASE WHEN cancel_requested THEN 'cancelled'
                    WHEN %s::text IS NOT NULL THEN 'failed' ELSE 'succeeded' END,
                stage='finished',
                error=CASE WHEN cancel_requested THEN NULL ELSE %s END,
                finished_at=now(), updated_at=now(), lease_token=NULL, lease_until=NULL
                WHERE id=%s AND status='running' AND lease_token=%s
                    AND lease_until > now()
                    AND (cancel_requested OR %s::text IS NOT NULL
                        OR completed_chunks=total_chunks)""",
                (error, error, job_id, token, error),
            )

    def cleanup(self, retention_days: int) -> None:
        with self._connect() as db:
            db.execute(
                """DELETE FROM daily_history_jobs WHERE finished_at IS NOT NULL
                AND finished_at < now() - (%s * interval '1 day')""",
                (retention_days,),
            )
            db.execute(
                """DELETE FROM daily_history_job_cache
                WHERE fetched_at < now() - (%s * interval '1 day')""",
                (retention_days,),
            )


def _bar_payload(bar: DailyBar) -> dict[str, Any]:
    return json.loads(
        json.dumps(
            asdict(bar),
            default=lambda value: (
                value.isoformat() if isinstance(value, (date, datetime)) else str(value)
            ),
        )
    )


def _run_job(
    repository: JobRepository,
    job: dict[str, Any],
    provider_factory: Callable[[], DailyHistoryProvider],
) -> None:
    job_id, token = job["id"], job["lease_token"]
    stopped = threading.Event()

    def keep_alive() -> None:
        while not stopped.wait(_HEARTBEAT_SECONDS):
            try:
                if not repository.heartbeat(job_id, token):
                    return
            except psycopg.Error:
                _LOG.warning("History job heartbeat failed")

    thread = threading.Thread(target=keep_alive, daemon=True)
    thread.start()
    try:
        provider = None
        for index in range(job["completed_chunks"], job["total_chunks"]):
            if not repository.set_stage(job_id, token, "checking_cache"):
                repository.finish(job_id, token)
                return
            year = job["start_date"].year + index
            start = max(job["start_date"], date(year, 1, 1))
            end = min(job["end_date"], date(year, 12, 31))
            bars = repository.cached(job["symbol"], start, end)
            fetched = bars is None
            if fetched:
                if not repository.set_stage(job_id, token, "querying"):
                    repository.finish(job_id, token)
                    return
                for attempt in range(_MAX_ATTEMPTS):
                    if repository.cancelled_or_lost(job_id, token):
                        repository.finish(job_id, token)
                        return
                    try:
                        if provider is None:
                            provider = provider_factory()
                        bars = [
                            _bar_payload(bar)
                            for bar in provider.get_daily_history(
                                job["symbol"], start, end
                            )
                        ]
                        break
                    except Exception as error:
                        cause = error
                        seen: set[int] = set()
                        while (cause.__cause__ or cause.__context__) and id(
                            cause
                        ) not in seen:
                            seen.add(id(cause))
                            next_cause = cause.__cause__ or cause.__context__
                            if not isinstance(next_cause, Exception):
                                break
                            cause = next_cause
                        category = _error_category(cause)
                        if isinstance(error, RecoverableNetworkError):
                            category = "network"
                        if category != "network" or attempt + 1 == _MAX_ATTEMPTS:
                            message = {
                                "network": "Daily history provider network unavailable",
                                "authentication": "Provider authentication failed",
                                "permission": "Provider permission denied",
                                "configuration": "Provider configuration invalid",
                            }.get(category, "Daily history retrieval failed")
                            repository.finish(job_id, token, message)
                            return
                        if stopped.wait(2**attempt):
                            return
            assert bars is not None
            if not repository.set_stage(job_id, token, "saving"):
                repository.finish(job_id, token)
                return
            if not repository.checkpoint(
                job_id, token, job["symbol"], start, end, bars, fetched
            ):
                repository.finish(job_id, token)
                return
        repository.finish(job_id, token)
    except psycopg.Error:
        raise
    except Exception:
        repository.finish(job_id, token, "Daily history processing failed")
    finally:
        stopped.set()
        thread.join()


def process_one(
    repository: JobRepository,
    provider_factory: Callable[[], DailyHistoryProvider] = create_provider_from_env,
) -> bool:
    """Claim and process one job, returning False when the queue is empty."""
    job = repository.claim()
    if job is None:
        return False
    _run_job(repository, job, provider_factory)
    return True


def worker_main() -> None:
    if os.environ.get("A500_HISTORY_PERSISTENCE_PERMITTED", "").lower() != "true":
        _LOG.info("History worker disabled: persistence permission not configured")
        return
    dsn = database_url()
    try:
        retention_days = int(os.environ.get("A500_JOB_RETENTION_DAYS", "30"))
        if retention_days < 1:
            raise ValueError
    except ValueError:
        raise SystemExit("A500_JOB_RETENTION_DAYS must be a positive integer") from None
    repository = JobRepository(dsn)
    last_cleanup = 0.0
    while True:
        try:
            if time.monotonic() - last_cleanup >= 3600:
                repository.cleanup(retention_days)
                last_cleanup = time.monotonic()
            if not process_one(repository):
                time.sleep(2)
        except psycopg.Error:
            _LOG.warning("History worker database operation failed; retrying")
            time.sleep(2)
        except Exception:
            # Do not print vendor errors or database connection strings.
            _LOG.error("History worker operation failed; retrying")
            time.sleep(2)


if __name__ == "__main__":
    worker_main()
