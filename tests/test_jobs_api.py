import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

import psycopg
from fastapi.testclient import TestClient

from a500_t_trader.jobs import JobRepository, process_one
from a500_t_trader.jobs_api import create_app
from a500_t_trader.market_data import SHANGHAI, DailyBar


def test_jobs_are_private_and_completed_dates_required(postgres_url: str) -> None:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    app = create_app(postgres_url, lambda token: token, persistence_permitted=True)
    with TestClient(app) as client:
        alice = {"Authorization": "Bearer alice"}
        bob = {"Authorization": "Bearer bob"}
        payload = {
            "type": "daily_history",
            "symbol": "563360",
            "start_date": "2000-01-01",
            "end_date": yesterday,
        }
        assert client.post("/v1/jobs", json=payload).status_code == 401
        created = client.post("/v1/jobs", json=payload, headers=alice)
        assert created.status_code == 202
        job_id = created.json()["id"]
        assert client.get(f"/v1/jobs/{job_id}", headers=bob).status_code == 404
        assert (
            client.get(f"/v1/jobs/{job_id}/results", headers=alice).status_code == 404
        )
        assert (
            client.post(f"/v1/jobs/{job_id}/cancel", headers=alice).status_code == 200
        )
        assert (
            client.get(f"/v1/jobs/{job_id}", headers=alice).json()["status"]
            == "cancelled"
        )
        payload["end_date"] = today.isoformat()
        assert client.post("/v1/jobs", json=payload, headers=alice).status_code == 422


def test_submission_requires_persistence_permission(postgres_url: str) -> None:
    app = create_app(postgres_url, lambda token: token, persistence_permitted=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            headers={"Authorization": "Bearer alice"},
            json={
                "type": "daily_history",
                "symbol": "563360",
                "start_date": "2020-01-01",
                "end_date": "2020-12-31",
            },
        )
    assert response.status_code == 503


def test_submission_persists_queued_job_with_pending_outbox(
    postgres_url: str,
) -> None:
    app = create_app(postgres_url, lambda token: token, persistence_permitted=True)
    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            headers={"Authorization": "Bearer alice"},
            json={
                "type": "daily_history",
                "symbol": "563360",
                "start_date": "2020-01-01",
                "end_date": "2020-12-31",
            },
        )

    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"
    with psycopg.connect(postgres_url) as db:
        persisted = db.execute(
            """SELECT j.status, o.published_at
            FROM daily_history_jobs j
            JOIN daily_history_job_outbox o ON o.job_id = j.id
            WHERE j.id = %s""",
            (job["id"],),
        ).fetchone()
    assert persisted == ("queued", None)


def test_job_publishes_shared_history_without_result_endpoint(
    postgres_url: str,
) -> None:
    class Provider:
        def get_daily_history(
            self, symbol: str, start_date: date, end_date: date
        ) -> tuple[DailyBar, ...]:
            return (
                DailyBar(
                    symbol=symbol,
                    timestamp=datetime(start_date.year, 1, 2, tzinfo=SHANGHAI),
                    open=Decimal("1.20"),
                    high=Decimal("1.30"),
                    low=Decimal("1.10"),
                    close=Decimal("1.25"),
                    volume=100,
                ),
            )

    app = create_app(postgres_url, lambda token: token, persistence_permitted=True)
    headers = {"Authorization": "Bearer alice"}
    with TestClient(app) as client:
        job = client.post(
            "/v1/jobs",
            headers=headers,
            json={
                "type": "daily_history",
                "symbol": "563360",
                "start_date": "2023-01-01",
                "end_date": "2024-12-31",
            },
        ).json()
        assert process_one(JobRepository(postgres_url), lambda: Provider())
        status = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()
        assert status["status"] == "succeeded"
        assert status["completed_chunks"] == status["total_chunks"] == 2
        assert not {"symbol", "start_date", "end_date"} & status.keys()
        assert not any("result" in key or "bar" in key for key in status)
        assert client.get(
            f"/v1/jobs/{job['id']}/results", headers=headers
        ).status_code == 404

    from a500_t_trader.backtest import BacktestRepository

    history = BacktestRepository(postgres_url).get_shared_daily_history(
        "563360", date(2023, 1, 1), date(2024, 12, 31)
    )
    assert history is not None
    assert [bar.timestamp.year for bar in history] == [2023, 2024]

def test_failed_job_discards_partial_staging_and_resubmission_reuses_cache(
    postgres_url: str,
) -> None:
    class Provider:
        fail = True
        calls: list[int] = []

        def get_daily_history(
            self, symbol: str, start_date: date, end_date: date
        ) -> tuple[DailyBar, ...]:
            self.calls.append(start_date.year)
            if start_date.year == 2024 and self.fail:
                raise PermissionError("secret credentials")
            bars = [
                DailyBar(
                    symbol=symbol,
                    timestamp=datetime(2023, 1, 2, tzinfo=SHANGHAI),
                    open=Decimal("1"),
                    high=Decimal("1"),
                    low=Decimal("1"),
                    close=Decimal("1"),
                    volume=100,
                ),
                DailyBar(
                    symbol=symbol,
                    timestamp=datetime(2023, 1, 3, tzinfo=SHANGHAI),
                    open=Decimal("2"),
                    high=Decimal("2"),
                    low=Decimal("2"),
                    close=Decimal("2"),
                    volume=200,
                ),
            ]
            if start_date.year == 2024:
                return (
                    DailyBar(
                        symbol=symbol,
                        timestamp=datetime(2024, 1, 2, tzinfo=SHANGHAI),
                        open=Decimal("1"),
                        high=Decimal("1"),
                        low=Decimal("1"),
                        close=Decimal("1"),
                        volume=100,
                    ),
                )
            return tuple(bars)

    provider = Provider()
    app = create_app(postgres_url, lambda token: token, persistence_permitted=True)
    headers = {"Authorization": "Bearer alice"}
    body = {
        "type": "daily_history",
        "symbol": "563360",
        "start_date": "2023-01-01",
        "end_date": "2024-12-31",
    }
    with TestClient(app) as client:
        first = client.post("/v1/jobs", headers=headers, json=body).json()
        assert process_one(JobRepository(postgres_url), lambda: provider)
        failed = client.get(f"/v1/jobs/{first['id']}", headers=headers).json()
        assert failed["status"] == "failed"
        assert failed["completed_chunks"] == 1
        assert "secret" not in failed["error"]
        assert client.get(
            f"/v1/jobs/{first['id']}/results", headers=headers
        ).status_code == 404
        from a500_t_trader.backtest import BacktestRepository

        BacktestRepository(postgres_url).save_daily_history(
            (
                DailyBar(
                    symbol="563360",
                    timestamp=datetime(2023, 1, 2, tzinfo=SHANGHAI),
                    open=Decimal("9"),
                    high=Decimal("9"),
                    low=Decimal("9"),
                    close=Decimal("9"),
                    volume=900,
                ),
            ),
            date(2023, 1, 1),
            date(2023, 12, 31),
        )
        provider.fail = False
        second = client.post("/v1/jobs", headers=headers, json=body).json()
        assert process_one(JobRepository(postgres_url), lambda: provider)
        assert client.get(
            f"/v1/jobs/{second['id']}/results", headers=headers
        ).status_code == 404
    from a500_t_trader.backtest import BacktestRepository

    history = BacktestRepository(postgres_url).get_shared_daily_history(
        "563360", date(2023, 1, 1), date(2024, 12, 31)
    )
    assert history is not None
    assert [
        (bar.timestamp.date(), bar.close) for bar in history
    ] == [
        (date(2023, 1, 2), Decimal("9")),
        (date(2023, 1, 3), Decimal("2")),
        (date(2024, 1, 2), Decimal("1")),
    ]
    with psycopg.connect(postgres_url) as db:
        staged = db.execute(
            "SELECT count(*) FROM daily_history_job_fetched_bars WHERE job_id=%s",
            (first["id"],),
        ).fetchone()[0]
    assert staged == 0
    assert provider.calls == [2023, 2024, 2024]



def test_running_job_reports_query_stage_and_cooperative_cancellation(
    postgres_url: str,
) -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[int] = []

    class Provider:
        def get_daily_history(
            self, symbol: str, start_date: date, end_date: date
        ) -> tuple[DailyBar, ...]:
            calls.append(start_date.year)
            entered.set()
            assert release.wait(5)
            return ()

    app = create_app(postgres_url, lambda token: token, persistence_permitted=True)
    headers = {"Authorization": "Bearer alice"}
    with TestClient(app) as client:
        job = client.post(
            "/v1/jobs",
            headers=headers,
            json={
                "type": "daily_history",
                "symbol": "563360",
                "start_date": "2023-01-01",
                "end_date": "2024-12-31",
            },
        ).json()
        worker = threading.Thread(
            target=process_one, args=(JobRepository(postgres_url), lambda: Provider())
        )
        worker.start()
        try:
            assert entered.wait(5)
            status = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()
            assert status["status"] == "running"
            assert status["stage"] == "querying"
            cancelled = client.post(
                f"/v1/jobs/{job['id']}/cancel", headers=headers
            ).json()
            assert cancelled["cancel_requested"] is True
        finally:
            release.set()
            worker.join(timeout=5)
        assert not worker.is_alive()
        status = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()
        assert status["status"] == "cancelled"
        assert calls == [2023]

def test_failed_publication_rolls_back_status_coverage_and_staging(
    postgres_url: str,
) -> None:
    from a500_t_trader.backtest import BacktestRepository

    BacktestRepository(postgres_url)
    app = create_app(postgres_url, lambda token: token, persistence_permitted=True)
    headers = {"Authorization": "Bearer alice"}

    class Provider:
        def get_daily_history(
            self, symbol: str, start_date: date, end_date: date
        ) -> tuple[DailyBar, ...]:
            return (
                DailyBar(
                    symbol=symbol,
                    timestamp=datetime(2023, 1, 2, tzinfo=SHANGHAI),
                    open=Decimal("1"),
                    high=Decimal("1"),
                    low=Decimal("1"),
                    close=Decimal("1"),
                    volume=1,
                ),
            )

    with TestClient(app) as client:
        job = client.post(
            "/v1/jobs",
            headers=headers,
            json={
                "type": "daily_history",
                "symbol": "563360",
                "start_date": "2023-01-01",
                "end_date": "2023-12-31",
            },
        ).json()
        with psycopg.connect(postgres_url) as db:
            db.execute(
                """CREATE FUNCTION reject_history_publish() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'blocked'; END $$"""
            )
            db.execute(
                """CREATE TRIGGER reject_history_publish BEFORE INSERT ON
                shared_daily_bars FOR EACH ROW EXECUTE FUNCTION reject_history_publish()"""
            )
        with pytest.raises(psycopg.Error):
            process_one(JobRepository(postgres_url), lambda: Provider())
        status = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()
        assert status["status"] == "running"
        assert BacktestRepository(postgres_url).get_shared_daily_history(
            "563360", date(2023, 1, 1), date(2023, 12, 31)
        ) is None
        with psycopg.connect(postgres_url) as db:
            published = db.execute(
                "SELECT count(*) FROM shared_daily_bars WHERE symbol=%s",
                ("563360",),
            ).fetchone()[0]
            staged = db.execute(
                "SELECT count(*) FROM daily_history_job_fetched_bars WHERE job_id=%s",
                (job["id"],),
            ).fetchone()[0]
            db.execute("DROP TRIGGER reject_history_publish ON shared_daily_bars")
            db.execute("DROP FUNCTION reject_history_publish()")
            db.execute(
                "UPDATE daily_history_jobs SET lease_until=now()-interval '1 second' "
                "WHERE id=%s",
                (job["id"],),
            )
        assert published == 0
        assert staged == 1
        assert process_one(JobRepository(postgres_url), lambda: Provider())
        status = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()
        assert status["status"] == "succeeded"
        assert BacktestRepository(postgres_url).get_shared_daily_history(
            "563360", date(2023, 1, 1), date(2023, 12, 31)
        ) is not None
