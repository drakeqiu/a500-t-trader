import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

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
            client.get(f"/v1/jobs/{job_id}/results", headers=alice).status_code == 409
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


def test_job_returns_paginated_snapshot_after_worker_execution(
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
        first = client.get(
            f"/v1/jobs/{job['id']}/results?limit=1&offset=0", headers=headers
        ).json()
        second = client.get(
            f"/v1/jobs/{job['id']}/results?limit=1&offset=1", headers=headers
        ).json()
        assert first["total"] == second["total"] == 2
        assert first["bars"][0]["close"] == "1.25"
        assert first["bars"][0]["timestamp"].startswith("2023-01-02")
        assert second["bars"][0]["timestamp"].startswith("2024-01-02")


def test_failed_job_hides_partial_results_and_resubmission_reuses_cache(
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
            return ()

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
        assert (
            client.get(f"/v1/jobs/{first['id']}/results", headers=headers).status_code
            == 409
        )
        provider.fail = False
        second = client.post("/v1/jobs", headers=headers, json=body).json()
        assert process_one(JobRepository(postgres_url), lambda: provider)
        result = client.get(f"/v1/jobs/{second['id']}/results", headers=headers)
        assert result.status_code == 200
        assert result.json()["bars"] == []
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
