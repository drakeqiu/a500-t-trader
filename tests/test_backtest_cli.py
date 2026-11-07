import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from _pytest.capture import CaptureFixture

from a500_t_trader.cli.main import main
from a500_t_trader.market_data import MinuteBar

TZ = ZoneInfo("Asia/Shanghai")


class HistoricalData:
    def __init__(self, bars: list[MinuteBar] | tuple[MinuteBar, ...]) -> None:
        self.bars = tuple(bars)
        self.request: tuple[str, date, date, int] | None = None

    def get_history_bars(
        self, symbol: str, start_date: date, end_date: date, interval_minutes: int
    ) -> tuple[MinuteBar, ...]:
        self.request = (symbol, start_date, end_date, interval_minutes)
        return self.bars


def bar(day: int, minute: int, price: str, *, completed: bool = True) -> MinuteBar:

    value = Decimal(price)
    return MinuteBar(
        "510300",
        datetime(2026, 10, day, 9, minute, tzinfo=TZ),
        1,
        value,
        value,
        value,
        value,
        1000,
        is_completed=completed,
    )


def test_backtest_cli_records_snapshot_constraints_baseline_and_assumptions(
    tmp_path: Path, capsys: CaptureFixture[str], postgres_url: str
) -> None:
    config = tmp_path / "rules.json"
    account = tmp_path / "account.json"
    config.write_text(
        '{"etfs":[{"symbol":"510300","buy_below":"9.50",'
        '"sell_above":"10.50","max_buy_cash":"10000",'
        '"minimum_holding":100}]}',
        encoding="utf-8",
    )
    account.write_text(
        '{"as_of":"2026-10-29T15:00:00+08:00","holdings":{"510300":1000},'
        '"cost_basis":{"510300":"10"},"sellable":{"510300":1000},'
        '"available_cash":"10000"}',
        encoding="utf-8",
    )
    data = HistoricalData(
        [
            bar(30, 0, "9.00"),
            bar(30, 1, "9.00"),
            bar(30, 2, "10.00"),
            bar(30, 3, "11.00"),
            bar(30, 4, "11.00"),
            bar(30, 5, "11.00"),
            bar(31, 0, "11.00"),
        ]
    )
    args = [
        "backtest",
        "--config",
        str(config),
        "--account",
        str(account),
        "--start-date",
        "2026-10-30",
        "--end-date",
        "2026-10-31",
        "--database",
        postgres_url,
        "--cost-bps",
        "10",
        "--slippage-bps",
        "5",
        "--minimum-commission",
        "0.1",
        "--implementation-version",
        "test-v1",
        "--confirm-history-persistence-permitted",
    ]

    assert main(args, market_data=data) == 0
    output = capsys.readouterr().out
    assert '"status": "completed"' in output
    assert '"implementation_version": "test-v1"' in output
    assert '"fill_assumption": "next_bar_open"' in output
    assert '"baseline"' in output and '"strategy"' in output
    assert '"snapshot_id"' in output
    assert '"simulated_fills"' in output
    assert data.request == ("510300", date(2026, 10, 30), date(2026, 10, 31), 1)
    result = json.loads(output)
    assert result["minimum_commission"] == "0.1"
    sells = [fill for fill in result["simulated_fills"] if fill["side"] == "sell"]
    assert [fill["quantity"] for fill in sells] == [1000, 1000]
    assert result["initial_account_snapshot"]["sellable"]["510300"] == 1000
    assert result["initial_account_snapshot"]["as_of"] == "2026-10-29T15:00:00+08:00"
    assert result["final_holdings"] == 100
    assert result["simulated_fills"][0]["status"] == "simulated; not an actual trade"
    assert result["strategy"]["final_value"] != result["baseline"]["final_value"]


def test_backtest_reuses_exactly_covered_shared_history(
    tmp_path: Path, capsys: CaptureFixture[str], postgres_url: str
) -> None:
    config = tmp_path / "rules.json"
    account = tmp_path / "account.json"
    config.write_text(
        '{"etfs":[{"symbol":"510300","buy_below":"9.50",'
        '"sell_above":"10.50","max_buy_cash":"10000","minimum_holding":100}]}',
        encoding="utf-8",
    )
    account.write_text(
        '{"as_of":"2026-10-29T15:00:00+08:00","holdings":{"510300":1000},'
        '"cost_basis":{"510300":"10"},"sellable":{"510300":1000},'
        '"available_cash":"10000"}',
        encoding="utf-8",
    )
    args = [
        "backtest",
        "--config",
        str(config),
        "--account",
        str(account),
        "--start-date",
        "2026-10-30",
        "--end-date",
        "2026-10-31",
        "--database",
        postgres_url,
        "--confirm-history-persistence-permitted",
    ]
    data = HistoricalData([bar(30, 0, "10"), bar(31, 0, "11")])
    assert main(args, market_data=data) == 0
    capsys.readouterr()

    class UnavailableData:
        def get_history_bars(
            self, symbol: str, start_date: date, end_date: date, interval_minutes: int
        ) -> tuple[MinuteBar, ...]:
            raise AssertionError("cached history should satisfy the exact request")

    assert main(args, market_data=UnavailableData()) == 0
    assert json.loads(capsys.readouterr().out)["snapshot_id"]


def test_backtest_cli_reports_unavailable_minute_history_without_fabricating(
    tmp_path: Path, capsys: CaptureFixture[str], postgres_url: str
) -> None:
    config = tmp_path / "rules.json"
    account = tmp_path / "account.json"
    config.write_text(
        '{"etfs":[{"symbol":"510300","buy_below":"9",'
        '"max_buy_cash":"1000","minimum_holding":100}]}',
        encoding="utf-8",
    )
    account.write_text(
        '{"as_of":"2026-10-29T15:00:00+08:00","holdings":{},'
        '"cost_basis":{},"sellable":{},"available_cash":"1000"}',
        encoding="utf-8",
    )

    class Unavailable:
        def get_history_bars(
            self, symbol: str, start_date: date, end_date: date, interval_minutes: int
        ) -> tuple[MinuteBar, ...]:

            from a500_t_trader.market_data import CapabilityUnavailableError

            raise CapabilityUnavailableError("minute history permission missing")

    args = [
        "backtest",
        "--config",
        str(config),
        "--account",
        str(account),
        "--start-date",
        "2026-10-30",
        "--end-date",
        "2026-10-30",
        "--database",
        postgres_url,
        "--confirm-history-persistence-permitted",
    ]
    assert main(args, market_data=Unavailable()) == 2
    assert "unavailable or unauthorized" in capsys.readouterr().err


def test_cleanup_preserves_backtest_referenced_minute_snapshot(
    tmp_path: Path, capsys: CaptureFixture[str], postgres_url: str
) -> None:
    config = tmp_path / "rules.json"
    account = tmp_path / "account.json"
    config.write_text(
        '{"etfs":[{"symbol":"510300","buy_below":"9",'
        '"max_buy_cash":"1000","minimum_holding":100}]}',
        encoding="utf-8",
    )
    account.write_text(
        '{"as_of":"2026-10-29T15:00:00+08:00","holdings":{},'
        '"cost_basis":{},"sellable":{},"available_cash":"1000"}',
        encoding="utf-8",
    )
    data = HistoricalData([bar(30, 0, "10"), bar(30, 1, "10")])
    args = [
        "backtest",
        "--config",
        str(config),
        "--account",
        str(account),
        "--start-date",
        "2026-10-30",
        "--end-date",
        "2026-10-30",
        "--database",
        postgres_url,
        "--confirm-history-persistence-permitted",
    ]
    assert main(args, market_data=data) == 0
    run = json.loads(capsys.readouterr().out)
    assert main(["history-cleanup", "--database", postgres_url], market_data=data) == 0
    assert '"deleted_shared_bars": 2' in capsys.readouterr().out
    assert (
        main(
            [
                "backtest-show",
                "--database",
                postgres_url,
                "--run-id",
                str(run["run_id"]),
            ]
        )
        == 0
    )
    shown = json.loads(capsys.readouterr().out)
    assert shown["snapshot_id"] == run["snapshot_id"]
    assert shown["bars"][0]["close"] == "10"


def test_backtest_requires_explicit_history_persistence_permission(
    tmp_path: Path, capsys: CaptureFixture[str], postgres_url: str
) -> None:
    config = tmp_path / "rules.json"
    account = tmp_path / "account.json"
    config.write_text(
        '{"etfs":[{"symbol":"510300","buy_below":"9",'
        '"max_buy_cash":"1000","minimum_holding":100}]}',
        encoding="utf-8",
    )
    account.write_text(
        '{"as_of":"2026-10-29T15:00:00+08:00","holdings":{},'
        '"cost_basis":{},"sellable":{},"available_cash":"1000"}',
        encoding="utf-8",
    )
    data = HistoricalData([bar(30, 0, "10"), bar(30, 1, "10")])
    args = [
        "backtest",
        "--config",
        str(config),
        "--account",
        str(account),
        "--start-date",
        "2026-10-30",
        "--end-date",
        "2026-10-30",
        "--database",
        postgres_url,
    ]

    assert main(args, market_data=data) == 2
    assert "persistence is not authorized" in capsys.readouterr().err
    assert data.request is None


def test_backtest_rejects_account_snapshot_after_first_history_bar(
    tmp_path: Path, capsys: CaptureFixture[str], postgres_url: str
) -> None:
    config = tmp_path / "rules.json"
    account = tmp_path / "account.json"
    config.write_text(
        '{"etfs":[{"symbol":"510300","buy_below":"9",'
        '"max_buy_cash":"1000","minimum_holding":100}]}',
        encoding="utf-8",
    )
    account.write_text(
        '{"as_of":"2026-10-30T09:31:00+08:00","holdings":{},'
        '"cost_basis":{},"sellable":{},"available_cash":"1000"}',
        encoding="utf-8",
    )
    data = HistoricalData([bar(30, 0, "10"), bar(30, 1, "10")])
    args = [
        "backtest",
        "--config",
        str(config),
        "--account",
        str(account),
        "--start-date",
        "2026-10-30",
        "--end-date",
        "2026-10-30",
        "--database",
        postgres_url,
        "--confirm-history-persistence-permitted",
    ]

    assert main(args, market_data=data) == 2
    assert (
        "initial account snapshot must not follow the first history bar"
        in capsys.readouterr().err
    )
