from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

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


def test_successful_job_persists_shared_history_and_snapshot_remains_subject_scoped(
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
        original = client.get(f"/v1/jobs/{job['id']}/results", headers=headers).json()
        assert original["total"] == 1
        assert (
            client.get(
                f"/v1/jobs/{job['id']}/results",
                headers={"Authorization": "Bearer bob"},
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
    refreshed = _bar("563360", 2, "9", year=2023)
    repository.save_daily_history((refreshed,), date(2023, 1, 1), date(2023, 12, 31))
    assert repository.get_shared_daily_history(
        "563360", date(2023, 1, 2), date(2023, 1, 2)
    ) == (refreshed,)

    class ProviderMustNotRun:
        def get_daily_history(
            self, symbol: str, start_date: date, end_date: date
        ) -> tuple[DailyBar, ...]:
            raise AssertionError("cached observations should avoid provider fetch")

    with TestClient(app) as client:
        cached_job = client.post(
            "/v1/jobs",
            headers=headers,
            json={
                "type": "daily_history",
                "symbol": "563360",
                "start_date": "2023-01-01",
                "end_date": "2024-12-31",
            },
        ).json()
        assert process_one(JobRepository(postgres_url), lambda: ProviderMustNotRun())
        cached_result = client.get(
            f"/v1/jobs/{cached_job['id']}/results", headers=headers
        ).json()
        assert cached_result["bars"][0]["close"] == "1"
        assert repository.get_shared_daily_history(
            "563360", date(2023, 1, 2), date(2023, 1, 2)
        ) == (refreshed,)
        unchanged = client.get(f"/v1/jobs/{job['id']}/results", headers=headers).json()
        assert unchanged == original
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
                "end_date": "2023-12-31",
            },
        ).json()

        class Provider:
            def get_daily_history(
                self, symbol: str, start_date: date, end_date: date
            ) -> tuple[DailyBar, ...]:
                assert (
                    client.post(
                        f"/v1/jobs/{job['id']}/cancel", headers=headers
                    ).status_code
                    == 200
                )
                return (_bar(symbol, 2, "1", year=2023),)

        assert process_one(JobRepository(postgres_url), lambda: Provider())
    repository = BacktestRepository(postgres_url)
    assert (
        repository.get_shared_daily_history(
            "563360", date(2023, 1, 1), date(2023, 12, 31)
        )
        is None
    )
