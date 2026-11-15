import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
import pytest
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


def test_recent_jobs_are_listed_for_the_authenticated_subject_only(
    postgres_url: str,
) -> None:
    app = create_app(postgres_url, lambda token: token, persistence_permitted=True)
    with TestClient(app) as client:
        alice = {"Authorization": "Bearer alice"}
        bob = {"Authorization": "Bearer bob"}
        payload = {
            "type": "daily_history",
            "symbol": "563360",
            "start_date": "2020-01-01",
            "end_date": "2020-12-31",
        }
        assert client.get("/v1/jobs").status_code == 401
        alice_first = client.post("/v1/jobs", json=payload, headers=alice).json()
        alice_second = client.post("/v1/jobs", json=payload, headers=alice).json()
        bob_job = client.post("/v1/jobs", json=payload, headers=bob).json()

        alice_jobs = client.get("/v1/jobs?limit=30&offset=0", headers=alice)
        bob_jobs = client.get("/v1/jobs?limit=30&offset=0", headers=bob)

    assert alice_jobs.status_code == 200
    assert [job["id"] for job in alice_jobs.json()["jobs"]] == [
        alice_second["id"],
        alice_first["id"],
    ]
    assert alice_jobs.json()["total"] == 2
    assert alice_jobs.json()["has_more"] is False
    assert [job["id"] for job in bob_jobs.json()["jobs"]] == [bob_job["id"]]


def test_job_list_pagination_and_subject_scoped_deletion(postgres_url: str) -> None:
    app = create_app(postgres_url, lambda token: token, persistence_permitted=True)
    with TestClient(app) as client:
        alice = {"Authorization": "Bearer alice"}
        bob = {"Authorization": "Bearer bob"}
        payload = {
            "type": "daily_history",
            "symbol": "563360",
            "start_date": "2020-01-01",
            "end_date": "2020-12-31",
        }
        jobs = [
            client.post("/v1/jobs", json=payload, headers=alice).json()
            for _ in range(3)
        ]
        for job in jobs:
            client.post(f"/v1/jobs/{job['id']}/cancel", headers=alice)

        page = client.get("/v1/jobs?limit=2&offset=0", headers=alice).json()
        next_page = client.get("/v1/jobs?limit=2&offset=2", headers=alice).json()
        assert len(page["jobs"]) == 2
        assert page["total"] == 3
        assert page["has_more"] is True
        assert len(next_page["jobs"]) == 1
        assert next_page["has_more"] is False

        assert (
            client.delete(f"/v1/jobs/{jobs[0]['id']}", headers=bob).status_code == 404
        )
        assert (
            client.delete(f"/v1/jobs/{jobs[0]['id']}", headers=alice).status_code == 204
        )
        assert client.get(f"/v1/jobs/{jobs[0]['id']}", headers=alice).status_code == 404
        assert (
            client.get("/v1/jobs?limit=30&offset=0", headers=alice).json()["total"] == 2
        )


def test_active_job_cannot_be_deleted(postgres_url: str) -> None:
    app = create_app(postgres_url, lambda token: token, persistence_permitted=True)
    with TestClient(app) as client:
        created = client.post(
            "/v1/jobs",
            headers={"Authorization": "Bearer alice"},
            json={
                "type": "daily_history",
                "symbol": "563360",
                "start_date": "2020-01-01",
                "end_date": "2020-12-31",
            },
        ).json()
        assert (
            client.delete(
                f"/v1/jobs/{created['id']}",
                headers={"Authorization": "Bearer alice"},
            ).status_code
            == 409
        )


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


def test_job_uses_clipped_calendar_quarters_for_creation_and_worker_progress(
    postgres_url: str,
) -> None:
    ranges: list[tuple[date, date]] = []
    progress_at_query: list[int] = []
    app = create_app(postgres_url, lambda token: token, persistence_permitted=True)
    headers = {"Authorization": "Bearer alice"}
    repository = JobRepository(postgres_url)

    class Provider:
        def get_daily_history(
            self, symbol: str, start_date: date, end_date: date
        ) -> tuple[DailyBar, ...]:
            ranges.append((start_date, end_date))
            if len(ranges) == 2:
                current = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()
                progress_at_query.append(current["completed_chunks"])
            return ()

    with TestClient(app) as client:
        job = client.post(
            "/v1/jobs",
            headers=headers,
            json={
                "type": "daily_history",
                "symbol": "563360",
                "start_date": "2023-02-15",
                "end_date": "2024-05-10",
            },
        ).json()
        assert job["total_chunks"] == 6
        assert job["completed_chunks"] == 0
        assert process_one(repository, lambda: Provider())
        status = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()

    assert ranges == [
        (date(2023, 2, 15), date(2023, 3, 31)),
        (date(2023, 4, 1), date(2023, 6, 30)),
        (date(2023, 7, 1), date(2023, 9, 30)),
        (date(2023, 10, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 3, 31)),
        (date(2024, 4, 1), date(2024, 5, 10)),
    ]
    assert progress_at_query == [1]
    assert status["status"] == "succeeded"
    assert status["completed_chunks"] == status["total_chunks"] == 6


def test_single_quarter_job_requests_only_its_clipped_dates(postgres_url: str) -> None:
    ranges: list[tuple[date, date]] = []

    class Provider:
        def get_daily_history(
            self, symbol: str, start_date: date, end_date: date
        ) -> tuple[DailyBar, ...]:
            ranges.append((start_date, end_date))
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
                "start_date": "2023-05-10",
                "end_date": "2023-06-15",
            },
        ).json()
        assert job["total_chunks"] == 1
        assert process_one(JobRepository(postgres_url), lambda: Provider())
        status = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()

    assert ranges == [(date(2023, 5, 10), date(2023, 6, 15))]
    assert status["status"] == "succeeded"
    assert status["completed_chunks"] == status["total_chunks"] == 1


def test_job_splits_adjacent_quarter_boundary_dates(
    postgres_url: str,
) -> None:
    ranges: list[tuple[date, date]] = []

    class Provider:
        def get_daily_history(
            self, symbol: str, start_date: date, end_date: date
        ) -> tuple[DailyBar, ...]:
            ranges.append((start_date, end_date))
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
                "start_date": "2023-03-31",
                "end_date": "2023-04-01",
            },
        ).json()
        assert job["total_chunks"] == 2
        assert process_one(JobRepository(postgres_url), lambda: Provider())
        status = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()

    assert ranges == [
        (date(2023, 3, 31), date(2023, 3, 31)),
        (date(2023, 4, 1), date(2023, 4, 1)),
    ]
    assert status["status"] == "succeeded"
    assert status["completed_chunks"] == status["total_chunks"] == 2


def test_worker_resumes_after_quarter_checkpoint_and_publishes_full_history(
    postgres_url: str,
) -> None:
    from a500_t_trader.backtest import BacktestRepository

    ranges: list[tuple[date, date]] = []
    quarter_bars = {
        date(2023, 1, 1): (date(2023, 1, 2), Decimal("1")),
        date(2023, 4, 1): (date(2023, 4, 2), Decimal("2")),
        date(2023, 7, 1): (date(2023, 7, 2), Decimal("3")),
        date(2023, 10, 1): (date(2023, 10, 2), Decimal("4")),
    }

    class Provider:
        def get_daily_history(
            self, symbol: str, start_date: date, end_date: date
        ) -> tuple[DailyBar, ...]:
            ranges.append((start_date, end_date))
            bar_date, close = quarter_bars[start_date]
            return (
                DailyBar(
                    symbol=symbol,
                    timestamp=datetime.combine(bar_date, datetime.min.time()).replace(
                        tzinfo=SHANGHAI
                    ),
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=100,
                ),
            )

    provider = Provider()
    repository = JobRepository(postgres_url)
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
                "end_date": "2023-12-31",
            },
        ).json()
        with psycopg.connect(postgres_url) as db:
            db.execute(
                """CREATE FUNCTION fail_test_quarter_checkpoint()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.bar_date = DATE '2023-04-02' THEN
                        RAISE EXCEPTION 'simulated checkpoint interruption'
                            USING ERRCODE='08006';
                    END IF;
                    RETURN NEW;
                END
                $$"""
            )
            db.execute(
                """CREATE TRIGGER fail_test_quarter_checkpoint
                BEFORE INSERT ON daily_history_job_fetched_bars
                FOR EACH ROW EXECUTE FUNCTION fail_test_quarter_checkpoint()"""
            )
        try:
            with pytest.raises(psycopg.Error):
                process_one(repository, lambda: provider)
        finally:
            with psycopg.connect(postgres_url) as db:
                db.execute(
                    "DROP TRIGGER IF EXISTS fail_test_quarter_checkpoint "
                    "ON daily_history_job_fetched_bars"
                )
                db.execute("DROP FUNCTION IF EXISTS fail_test_quarter_checkpoint()")

        interrupted = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()
        assert interrupted["status"] == "running"
        assert interrupted["completed_chunks"] == 1
        with psycopg.connect(postgres_url) as db:
            db.execute(
                """UPDATE daily_history_jobs SET lease_until=now() - interval '1 second'
                WHERE id=%s""",
                (job["id"],),
            )
        assert process_one(repository, lambda: provider)
        completed = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()

    history = BacktestRepository(postgres_url).get_shared_daily_history(
        "563360", date(2023, 1, 1), date(2023, 12, 31)
    )
    assert completed["status"] == "succeeded"
    assert completed["completed_chunks"] == completed["total_chunks"] == 4
    assert ranges == [
        (date(2023, 1, 1), date(2023, 3, 31)),
        (date(2023, 4, 1), date(2023, 6, 30)),
        (date(2023, 4, 1), date(2023, 6, 30)),
        (date(2023, 7, 1), date(2023, 9, 30)),
        (date(2023, 10, 1), date(2023, 12, 31)),
    ]
    assert history is not None
    assert [(bar.timestamp.date(), bar.close) for bar in history] == [
        (date(2023, 1, 2), Decimal("1")),
        (date(2023, 4, 2), Decimal("2")),
        (date(2023, 7, 2), Decimal("3")),
        (date(2023, 10, 2), Decimal("4")),
    ]


def test_legacy_annual_checkpoint_migrates_before_quarterly_resume(
    postgres_url: str,
) -> None:
    ranges: list[tuple[date, date]] = []
    JobRepository(postgres_url)
    with psycopg.connect(postgres_url) as db:
        db.execute("DELETE FROM daily_history_job_migrations")
        db.execute(
            "ALTER TABLE daily_history_jobs "
            "DROP CONSTRAINT daily_history_jobs_quarter_chunks"
        )
        inserted = db.execute(
            """INSERT INTO daily_history_jobs
            (id, subject, symbol, start_date, end_date, status,
                total_chunks, completed_chunks, lease_token, lease_until)
            VALUES (gen_random_uuid(), 'alice', '563360', '2023-01-01',
                '2024-12-31', 'running', 2, 1, gen_random_uuid(),
                now() + interval '1 hour') RETURNING id"""
        ).fetchone()
    assert inserted is not None
    job_id = inserted[0]

    class Provider:
        def get_daily_history(
            self, symbol: str, start_date: date, end_date: date
        ) -> tuple[DailyBar, ...]:
            ranges.append((start_date, end_date))
            return ()

    migrated_repository = JobRepository(postgres_url)
    with psycopg.connect(postgres_url) as db:
        checkpoint = db.execute(
            "SELECT total_chunks, completed_chunks, lease_token, lease_until "
            "FROM daily_history_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
    assert checkpoint == (8, 4, None, None)
    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(postgres_url) as db:
            db.execute(
                """INSERT INTO daily_history_jobs
                (id, subject, symbol, start_date, end_date, status, total_chunks)
                VALUES (gen_random_uuid(), 'old-api', '563360', '2023-01-01',
                    '2024-12-31', 'queued', 2)"""
            )
    assert process_one(migrated_repository, lambda: Provider())
    with psycopg.connect(postgres_url) as db:
        completed = db.execute(
            "SELECT status, total_chunks, completed_chunks "
            "FROM daily_history_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()

    assert completed == ("succeeded", 8, 8)
    assert ranges == [
        (date(2024, 1, 1), date(2024, 3, 31)),
        (date(2024, 4, 1), date(2024, 6, 30)),
        (date(2024, 7, 1), date(2024, 9, 30)),
        (date(2024, 10, 1), date(2024, 12, 31)),
    ]


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
        assert status["completed_chunks"] == status["total_chunks"] == 8
        assert not {"symbol", "start_date", "end_date"} & status.keys()
        assert not any("result" in key or "bar" in key for key in status)
        assert (
            client.get(f"/v1/jobs/{job['id']}/results", headers=headers).status_code
            == 404
        )

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
        assert failed["completed_chunks"] == 4
        assert "secret" not in failed["error"]
        assert (
            client.get(f"/v1/jobs/{first['id']}/results", headers=headers).status_code
            == 404
        )
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
        assert (
            client.get(f"/v1/jobs/{second['id']}/results", headers=headers).status_code
            == 404
        )
    from a500_t_trader.backtest import BacktestRepository

    history = BacktestRepository(postgres_url).get_shared_daily_history(
        "563360", date(2023, 1, 1), date(2024, 12, 31)
    )
    assert history is not None
    assert [(bar.timestamp.date(), bar.close) for bar in history] == [
        (date(2023, 1, 2), Decimal("9")),
        (date(2023, 1, 3), Decimal("2")),
        (date(2024, 1, 2), Decimal("1")),
    ]
    with psycopg.connect(postgres_url) as db:
        staged_row = db.execute(
            "SELECT count(*) FROM daily_history_job_fetched_bars WHERE job_id=%s",
            (first["id"],),
        ).fetchone()
    assert staged_row is not None
    assert staged_row[0] == 0
    assert provider.calls == [2023] * 4 + [2024] + [2024] * 4


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
                shared_daily_bars FOR EACH ROW EXECUTE FUNCTION
                reject_history_publish()"""
            )
        with pytest.raises(psycopg.Error):
            process_one(JobRepository(postgres_url), lambda: Provider())
        status = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()
        assert status["status"] == "running"
        assert (
            BacktestRepository(postgres_url).get_shared_daily_history(
                "563360", date(2023, 1, 1), date(2023, 12, 31)
            )
            is None
        )
        with psycopg.connect(postgres_url) as db:
            published_row = db.execute(
                "SELECT count(*) FROM shared_daily_bars WHERE symbol=%s",
                ("563360",),
            ).fetchone()
            staged_row = db.execute(
                "SELECT count(*) FROM daily_history_job_fetched_bars WHERE job_id=%s",
                (job["id"],),
            ).fetchone()
            assert published_row is not None
            assert staged_row is not None
            published = published_row[0]
            staged = staged_row[0]
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
        assert (
            BacktestRepository(postgres_url).get_shared_daily_history(
                "563360", date(2023, 1, 1), date(2023, 12, 31)
            )
            is not None
        )
