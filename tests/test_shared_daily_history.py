from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
from fastapi.testclient import TestClient

from a500_t_trader.backtest import BacktestRepository
from a500_t_trader.jobs import JobRepository, process_one
from a500_t_trader.jobs_api import create_app
from a500_t_trader.market_data import DailyBar

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _bar(symbol: str, day: int, close: str, year: int = 2026) -> DailyBar:
    return DailyBar(
        symbol,
        datetime(year, 1, day, tzinfo=SHANGHAI),
        Decimal(close),
        Decimal(close),
        Decimal(close),
        Decimal(close),
        100,
    )


def test_shared_daily_history_merges_coverage_and_replaces_observations(
    postgres_url: str,
) -> None:
    repository = BacktestRepository(postgres_url)
    start, end = date(2026, 1, 1), date(2026, 1, 6)
    assert repository.get_shared_daily_history("510300", start, end) is None
    repository.save_daily_history((_bar("510300", 2, "2"),), start, date(2026, 1, 2))
    repository.save_daily_history((_bar("510300", 5, "5"),), date(2026, 1, 5), end)
    assert repository.get_shared_daily_history("510300", start, end) is None
    repository.save_daily_history(
        (), date(2026, 1, 3), date(2026, 1, 4), symbol="510300"
    )
    rows = repository.get_shared_daily_history("510300", start, end)
    assert rows is not None
    assert [row.timestamp.day for row in rows] == [2, 5]
    assert rows[0].close == Decimal("2")
    alternate_timestamp = datetime(2026, 1, 2, 15, tzinfo=SHANGHAI)
    later_observation = replace(_bar("510300", 2, "20"), timestamp=alternate_timestamp)
    repository.save_daily_history(
        (_bar("510300", 5, "5"), later_observation), start, end
    )
    rows = repository.get_shared_daily_history("510300", start, end)
    assert rows is not None
    assert [row.timestamp.day for row in rows] == [2, 5]
    assert rows[0].timestamp.hour == 15
    assert rows[0].close == Decimal("20")
    assert repository.get_shared_daily_history("510301", start, end) is None


def test_shared_daily_history_distinguishes_gaps_and_includes_boundaries(
    postgres_url: str,
) -> None:
    repository = BacktestRepository(postgres_url)
    repository.save_daily_history(
        (), date(2026, 1, 1), date(2026, 1, 2), symbol="510301"
    )
    repository.save_daily_history(
        (), date(2026, 1, 4), date(2026, 1, 5), symbol="510301"
    )
    assert (
        repository.get_shared_daily_history(
            "510301", date(2026, 1, 1), date(2026, 1, 5)
        )
        is None
    )
    assert (
        repository.get_shared_daily_history(
            "510301", date(2026, 1, 1), date(2026, 1, 2)
        )
        == ()
    )
    repository.save_daily_history(
        (), date(2026, 1, 3), date(2026, 1, 3), symbol="510301"
    )
    assert (
        repository.get_shared_daily_history(
            "510301", date(2026, 1, 1), date(2026, 1, 5)
        )
        == ()
    )


def test_shared_daily_history_merges_overlapping_ranges(postgres_url: str) -> None:
    repository = BacktestRepository(postgres_url)
    repository.save_daily_history(
        (), date(2026, 1, 1), date(2026, 1, 4), symbol="510302"
    )
    repository.save_daily_history(
        (), date(2026, 1, 3), date(2026, 1, 6), symbol="510302"
    )
    assert (
        repository.get_shared_daily_history(
            "510302", date(2026, 1, 1), date(2026, 1, 6)
        )
        == ()
    )


def test_successful_job_publishes_history_and_preserves_empty_query_coverage(
    postgres_url: str,
) -> None:
    class Provider:
        def get_daily_history(
            self, symbol: str, start_date: date, end_date: date
        ) -> tuple[DailyBar, ...]:
            if start_date.year == 2023:
                return (_bar(symbol, 2, "1", year=2023),)
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
        assert process_one(JobRepository(postgres_url), lambda: Provider())
        status = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()
        assert status["status"] == "succeeded"
        assert not any("result" in key or "bar" in key for key in status)
        assert (
            client.get(f"/v1/jobs/{job['id']}/results", headers=headers).status_code
            == 404
        )
        assert (
            client.get(
                f"/v1/jobs/{job['id']}", headers={"Authorization": "Bearer bob"}
            ).status_code
            == 404
        )

    repository = BacktestRepository(postgres_url)
    shared = repository.get_shared_daily_history(
        "563360", date(2023, 1, 1), date(2024, 12, 31)
    )
    assert shared is not None and [bar.timestamp.year for bar in shared] == [2023]
    assert (
        repository.get_shared_daily_history(
            "563360", date(2024, 1, 1), date(2024, 12, 31)
        )
        == ()
    )
    with psycopg.connect(postgres_url) as db:
        staged_row = db.execute(
            "SELECT count(*) FROM daily_history_job_fetched_bars WHERE job_id=%s",
            (job["id"],),
        ).fetchone()
        result_table_row = db.execute(
            "SELECT to_regclass('daily_history_job_bars')"
        ).fetchone()
    assert staged_row is not None
    assert result_table_row is not None
    assert staged_row[0] == 0
    assert result_table_row[0] is None

    refreshed = _bar("563360", 2, "9", year=2023)
    repository.save_daily_history((refreshed,), date(2023, 1, 1), date(2023, 12, 31))
    assert repository.get_shared_daily_history(
        "563360", date(2023, 1, 2), date(2023, 1, 2)
    ) == (refreshed,)
    JobRepository(postgres_url).cleanup(0)
    assert (
        repository.get_shared_daily_history(
            "563360", date(2023, 1, 1), date(2024, 12, 31)
        )
        is not None
    )


def test_failed_job_does_not_publish_partial_shared_history(
    postgres_url: str,
) -> None:
    class Provider:
        def get_daily_history(
            self, symbol: str, start_date: date, end_date: date
        ) -> tuple[DailyBar, ...]:
            if start_date.year == 2024:
                raise PermissionError("provider denied")
            return (_bar(symbol, 2, "1", year=2023),)

    app = create_app(postgres_url, lambda token: token, persistence_permitted=True)
    with TestClient(app) as client:
        client.post(
            "/v1/jobs",
            headers={"Authorization": "Bearer alice"},
            json={
                "type": "daily_history",
                "symbol": "563360",
                "start_date": "2023-01-01",
                "end_date": "2024-12-31",
            },
        )
        process_one(JobRepository(postgres_url), lambda: Provider())
    repository = BacktestRepository(postgres_url)
    assert (
        repository.get_shared_daily_history(
            "563360", date(2023, 1, 1), date(2024, 12, 31)
        )
        is None
    )


def test_cancelled_job_does_not_publish_shared_history(postgres_url: str) -> None:
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

        class Provider:
            def get_daily_history(
                self, symbol: str, start_date: date, end_date: date
            ) -> tuple[DailyBar, ...]:
                if start_date.year == 2024:
                    assert (
                        client.post(
                            f"/v1/jobs/{job['id']}/cancel", headers=headers
                        ).status_code
                        == 200
                    )
                return (_bar(symbol, 2, "1", year=start_date.year),)

        assert process_one(JobRepository(postgres_url), lambda: Provider())
    repository = BacktestRepository(postgres_url)
    assert (
        repository.get_shared_daily_history(
            "563360", date(2023, 1, 1), date(2023, 12, 31)
        )
        is None
    )
    with psycopg.connect(postgres_url) as db:
        staged_row = db.execute(
            "SELECT count(*) FROM daily_history_job_fetched_bars WHERE job_id=%s",
            (job["id"],),
        ).fetchone()
    assert staged_row is not None
    assert staged_row[0] == 0


def test_authenticated_daily_history_symbols_list_shared_coverage(
    postgres_url: str,
) -> None:
    repository = BacktestRepository(postgres_url)
    repository.save_daily_history(
        (), date(2026, 1, 1), date(2026, 1, 5), symbol="510300"
    )
    repository.save_daily_history(
        (), date(2026, 1, 4), date(2026, 1, 8), symbol="510300"
    )
    repository.save_daily_history(
        (), date(2025, 12, 1), date(2026, 1, 9), symbol="510500"
    )
    app = create_app(postgres_url, lambda token: token)
    with TestClient(app) as client:
        assert client.get("/v1/history/daily/symbols").status_code == 401
        response = client.get(
            "/v1/history/daily/symbols",
            headers={"Authorization": "Bearer alice"},
        )
        assert client.get("/v1/history/daily/symbols/510300/ranges").status_code == 401
        ranges = client.get(
            "/v1/history/daily/symbols/SH.510300/ranges",
            headers={"Authorization": "Bearer alice"},
        )
    assert response.status_code == 200
    assert response.json()["symbols"] == [
        {"symbol": "510300", "start_date": "2026-01-01", "end_date": "2026-01-08"},
        {"symbol": "510500", "start_date": "2025-12-01", "end_date": "2026-01-09"},
    ]
    assert ranges.status_code == 200
    assert ranges.json() == {
        "symbol": "510300",
        "ranges": [{"start_date": "2026-01-01", "end_date": "2026-01-08"}],
    }


def test_authenticated_daily_history_api_reports_coverage_and_pages_rows(
    postgres_url: str,
) -> None:
    repository = BacktestRepository(postgres_url)
    start, end = date(2026, 1, 1), date(2026, 1, 5)
    repository.save_daily_history(
        (_bar("510300", 2, "2"), _bar("510300", 5, "5")),
        start,
        end,
    )
    app = create_app(postgres_url, lambda token: token)
    with TestClient(app) as client:
        params: dict[str, str | int] = {
            "symbol": "SH.510300",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "limit": 1,
            "offset": 0,
        }
        assert client.get("/v1/history/daily", params=params).status_code == 401
        first = client.get(
            "/v1/history/daily",
            params=params,
            headers={"Authorization": "Bearer alice"},
        )
        second = client.get(
            "/v1/history/daily",
            params={**params, "offset": 1},
            headers={"Authorization": "Bearer alice"},
        )
        uncovered = client.get(
            "/v1/history/daily",
            params={**params, "symbol": "510301"},
            headers={"Authorization": "Bearer alice"},
        )
        invalid = client.get(
            "/v1/history/daily",
            params={**params, "symbol": "invalid"},
            headers={"Authorization": "Bearer alice"},
        )

    assert first.status_code == 200
    assert first.json()["covered"] is True
    assert first.json()["total"] == 2
    assert first.json()["bars"][0]["date"] == "2026-01-02"
    assert first.json()["bars"][0]["close"] == "2"
    assert second.json()["bars"][0]["date"] == "2026-01-05"
    assert uncovered.json()["covered"] is False
    assert uncovered.json()["bars"] == []
    assert invalid.status_code == 422


def test_daily_history_api_distinguishes_covered_empty_from_uncovered(
    postgres_url: str,
) -> None:
    BacktestRepository(postgres_url).save_daily_history(
        (), date(2026, 1, 1), date(2026, 1, 5), symbol="510300"
    )
    app = create_app(postgres_url, lambda token: token)
    with TestClient(app) as client:
        response = client.get(
            "/v1/history/daily",
            params={
                "symbol": "510300",
                "start_date": "2026-01-01",
                "end_date": "2026-01-05",
            },
            headers={"Authorization": "Bearer alice"},
        )
    assert response.status_code == 200
    assert response.json()["covered"] is True
    assert response.json()["total"] == 0
    assert response.json()["bars"] == []
