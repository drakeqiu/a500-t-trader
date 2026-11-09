from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from a500_t_trader.backtest import BacktestRepository
from a500_t_trader.market_data import DailyBar

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _bar(symbol: str, day: int, close: str) -> DailyBar:
    return DailyBar(
        symbol,
        datetime(2026, 1, day, tzinfo=SHANGHAI),
        Decimal(close),
        Decimal(close),
        Decimal(close),
        Decimal(close),
        100,
    )


def test_shared_daily_history_merges_coverage_and_upserts_observations(
    postgres_url: str,
) -> None:
    repository = BacktestRepository(postgres_url)
    start, end = date(2026, 1, 1), date(2026, 1, 6)
    assert repository.get_shared_daily_history("510300", start, end) is None
    repository.save_daily_history((_bar("510300", 2, "2"),), start, date(2026, 1, 2))
    repository.save_daily_history((_bar("510300", 5, "5"),), date(2026, 1, 5), end)
    assert repository.get_shared_daily_history("510300", start, end) is None
    repository.save_daily_history((), date(2026, 1, 3), date(2026, 1, 4), symbol="510300")
    rows = repository.get_shared_daily_history("510300", start, end)
    assert rows is not None
    assert [row.timestamp.day for row in rows] == [2, 3]
    assert rows[0].close == Decimal("2")
    repository.save_daily_history((_bar("510300", 2, "20"),), start, end)
    rows = repository.get_shared_daily_history("510300", start, end)
    assert rows is not None and rows[0].close == Decimal("20")
    assert repository.get_shared_daily_history("510301", start, end) is None


def test_shared_daily_history_merges_overlapping_ranges(postgres_url: str) -> None:
    repository = BacktestRepository(postgres_url)
    repository.save_daily_history(
        (), date(2026, 1, 1), date(2026, 1, 4), symbol="510301"
    )
    repository.save_daily_history(
        (), date(2026, 1, 3), date(2026, 1, 6), symbol="510301"
    )
    assert repository.get_shared_daily_history(
        "510301", date(2026, 1, 1), date(2026, 1, 6)
    ) == ()


def test_successful_job_persists_shared_history_and_snapshot_remains_subject_scoped(
    postgres_url: str,
) -> None:
    from fastapi.testclient import TestClient

    from a500_t_trader.jobs import JobRepository, create_app, process_one

    class Provider:
        def get_daily_history(self, symbol: str, start_date: date, end_date: date) -> tuple[DailyBar, ...]:
            if start_date.year == 2023:
                return (_bar(symbol, 2, "1"),)
            return ()

    app = create_app(postgres_url, lambda token: token, persistence_permitted=True)
    headers = {"Authorization": "Bearer alice"}
    with TestClient(app) as client:
        job = client.post("/v1/jobs", headers=headers, json={
            "type": "daily_history", "symbol": "563360",
            "start_date": "2023-01-01", "end_date": "2024-12-31",
        }).json()
        assert process_one(JobRepository(postgres_url), lambda: Provider())
        original = client.get(f"/v1/jobs/{job['id']}/results", headers=headers).json()
        assert original["total"] == 1
        assert client.get(f"/v1/jobs/{job['id']}/results", headers={"Authorization": "Bearer bob"}).status_code == 404
    repository = BacktestRepository(postgres_url)
    shared = repository.get_shared_daily_history("563360", date(2023, 1, 1), date(2024, 12, 31))
    assert shared is not None and [bar.timestamp.year for bar in shared] == [2023]
    assert repository.get_shared_daily_history("563360", date(2024, 1, 1), date(2024, 12, 31)) == ()
    repository.save_daily_history((_bar("563360", 2, "9"),), date(2023, 1, 1), date(2023, 12, 31))
    with TestClient(app) as client:
        unchanged = client.get(f"/v1/jobs/{job['id']}/results", headers=headers).json()
        assert unchanged == original
    JobRepository(postgres_url).cleanup(0)
    assert repository.get_shared_daily_history("563360", date(2023, 1, 1), date(2024, 12, 31)) is not None
