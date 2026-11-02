import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from a500_t_trader.cli.main import main
from a500_t_trader.market_data import MinuteBar, Snapshot
from a500_t_trader.market_data.trading_calendar import SessionState

SHANGHAI = ZoneInfo("Asia/Shanghai")
MORNING = datetime(2026, 10, 30, 10, 0, tzinfo=SHANGHAI)
CLOSE = datetime(2026, 10, 30, 15, 0, tzinfo=SHANGHAI)


class Data:
    def get_snapshot(self, symbol: str) -> Snapshot:
        return Snapshot(symbol, MORNING, MORNING, last_price=Decimal("3.80"))

    def session_status(self, session_date: date) -> tuple[SessionState, bool]:
        return SessionState.TRADING, False

    def get_intraday_bars(
        self, symbol: str, interval_minutes: int = 1
    ) -> tuple[MinuteBar, ...]:
        return (
            MinuteBar(
                symbol,
                datetime(2026, 10, 30, 9, 59, tzinfo=SHANGHAI),
                1,
                Decimal("3.80"),
                Decimal("3.81"),
                Decimal("3.79"),
                Decimal("3.80"),
                1000,
            ),
        )

    def get_history_bars(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        interval_minutes: int,
    ) -> tuple[MinuteBar, ...]:
        return (
            MinuteBar(
                symbol,
                datetime(2026, 10, 30, 9, 59, tzinfo=SHANGHAI),
                1,
                Decimal("3.80"),
                Decimal("3.81"),
                Decimal("3.79"),
                Decimal("3.80"),
                1000,
            ),
        )


def test_recorded_fixture_premarket_then_postmarket_is_advisory(
    tmp_path: Path, capsys: Any
) -> None:
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "amazingdata_563360_20261102.json"
        ).read_text(encoding="utf-8")
    )
    recorded = fixture["snapshot"]
    snapshot_time = datetime.fromisoformat(recorded["timestamp"])
    snapshot = Snapshot(
        recorded["symbol"],
        snapshot_time,
        datetime.fromisoformat(recorded["received_at"]),
        last_price=Decimal(recorded["last_price"]),
        previous_close=Decimal(recorded["previous_close"]),
        open=Decimal(recorded["open"]),
        high=Decimal(recorded["high"]),
        low=Decimal(recorded["low"]),
        cumulative_volume=recorded["cumulative_volume"],
        cumulative_amount=Decimal(recorded["cumulative_amount"]),
        bid_price=Decimal(recorded["bid_price"]),
        bid_volume=recorded["bid_volume"],
        ask_price=Decimal(recorded["ask_price"]),
        ask_volume=recorded["ask_volume"],
        source=recorded["source"],
    )
    bars = tuple(
        MinuteBar(
            row["symbol"],
            datetime.fromisoformat(row["timestamp"]),
            row["interval_minutes"],
            Decimal(row["open"]),
            Decimal(row["high"]),
            Decimal(row["low"]),
            Decimal(row["close"]),
            row["volume"],
            Decimal(row["amount"]),
            row["is_completed"],
            row["source"],
        )
        for row in fixture["bars"]
    )

    class RecordedData:
        def get_snapshot(self, symbol: str) -> Snapshot:
            assert symbol == snapshot.symbol
            return snapshot

        def session_status(self, session_date: date) -> tuple[SessionState, bool]:
            assert session_date == snapshot_time.date()
            return SessionState.TRADING, False

        def get_intraday_bars(
            self, symbol: str, interval_minutes: int = 1
        ) -> tuple[MinuteBar, ...]:
            assert symbol == snapshot.symbol
            assert interval_minutes == 1
            return bars

        def get_history_bars(
            self,
            symbol: str,
            start_date: date,
            end_date: date,
            interval_minutes: int,
        ) -> tuple[MinuteBar, ...]:
            assert symbol == snapshot.symbol
            assert start_date == end_date == snapshot_time.date()
            assert interval_minutes == 1
            return bars

    data = RecordedData()
    config = tmp_path / "rules.json"
    account = tmp_path / "account.json"
    plan_file = tmp_path / "plan.json"
    database = tmp_path / "review.sqlite3"
    config.write_text(
        '{"etfs":[{"symbol":"563360","buy_below":"1.10",'
        '"max_buy_cash":"10000","minimum_holding":100}]}',
        encoding="utf-8",
    )
    account.write_text(
        '{"as_of":"2026-11-02T10:55:00+08:00","holdings":{"563360":10000},'
        '"cost_basis":{"563360":"1.05"},"sellable":{"563360":8000},'
        '"available_cash":"20000"}',
        encoding="utf-8",
    )

    assert main(
        [
            "premarket",
            "--config",
            str(config),
            "--account",
            str(account),
            "--output",
            str(plan_file),
        ],
        market_data=data,
        as_of=snapshot_time,
    ) == 0
    planned = json.loads(capsys.readouterr().out)
    assert plan_file.is_file()
    assert planned["suggestions"][0]["direction"] == "buy"
    assert planned["suggestions"][0]["quantity"] > 0
    assert planned["suggestions"][0]["symbol"] == "563360"

    assert main(
        [
            "postmarket",
            "--date",
            snapshot_time.date().isoformat(),
            "--plan",
            str(plan_file),
            "--database",
            str(database),
        ],
        market_data=data,
        as_of=snapshot_time.replace(hour=15, minute=0),
    ) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["session_date"] == snapshot_time.date().isoformat()
    assert review["plan"]["suggestions"][0]["direction"] == "buy"
    assert review["plan"]["suggestions"][0]["quantity"] == (
        planned["suggestions"][0]["quantity"]
    )
    closing_bar = bars[-1]
    assert review["market_data"]["563360"]["close"] == str(closing_bar.close)
    assert review["market_data"]["563360"]["bar_timestamp"] == (
        closing_bar.timestamp.isoformat()
    )
    assert review["market_data"]["563360"]["is_completed"] is True
    assert review["simulated_fills"] == {"status": "not recorded", "records": []}
    assert "order" in review["recommendations"]["classification"].lower()
    assert "not orders" in review["recommendations"]["classification"]


def test_cli_daily_workflow_saves_plan_triggers_and_reviews_without_inventing_trades(
    tmp_path: Path, capsys: Any
) -> None:
    config = tmp_path / "rules.json"
    account = tmp_path / "account.json"
    plan_file = tmp_path / "plan.json"
    database = tmp_path / "signals.sqlite3"
    config.write_text(
        '{"etfs":[{"symbol":"510300","buy_below":"3.85",'
        '"max_buy_cash":"4000","minimum_holding":100}]}',
        encoding="utf-8",
    )
    account.write_text(
        '{"as_of":"2026-10-30T10:00:00+08:00","holdings":{"510300":1200},'
        '"cost_basis":{"510300":"3.70"},"sellable":{"510300":800},'
        '"available_cash":"5000"}',
        encoding="utf-8",
    )
    data = Data()

    assert (
        main(
            [
                "premarket",
                "--config",
                str(config),
                "--account",
                str(account),
                "--output",
                str(plan_file),
            ],
            market_data=data,
            as_of=MORNING,
        )
        == 0
    )
    assert '"direction": "buy"' in capsys.readouterr().out

    assert (
        main(
            [
                "monitor",
                "--config",
                str(config),
                "--account",
                str(account),
                "--database",
                str(database),
                "--once",
            ],
            market_data=data,
            as_of=datetime(2026, 10, 30, 10, 1, tzinfo=SHANGHAI),
        )
        == 0
    )
    assert "NOT AN ORDER" in capsys.readouterr().out
    assert (
        main(
            [
                "manual-fill",
                "--timestamp",
                "2026-10-30T10:02:00+08:00",
                "--symbol",
                "510300",
                "--side",
                "buy",
                "--quantity",
                "100",
                "--price",
                "3.80",
                "--database",
                str(database),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "postmarket",
                "--date",
                "2026-10-30",
                "--plan",
                str(plan_file),
                "--database",
                str(database),
            ],
            market_data=data,
            as_of=CLOSE,
        )
        == 0
    )
    review = json.loads(capsys.readouterr().out)
    assert review["session_date"] == "2026-10-30"
    assert review["plan"]["suggestions"][0]["direction"] == "buy"
    assert review["triggers"][0]["condition"] == "low <= 3.85 CNY"
    assert review["account_snapshot"]["holdings"]["510300"] == 1200
    assert review["triggers"][0]["related_recommendation_indices"] == [0]
    assert review["market_data"]["510300"]["close"] == "3.80"
    assert review["account_snapshot"]["source"] == "user-entered; not broker-verified"
    assert review["manual_fills"]["status"] == "recorded"
    assert review["manual_fills"]["records"][0]["quantity"] == 100
    assert review["manual_fills"]["records"][0]["classification"] == (
        "user-entered manual fill; not broker-verified"
    )
    assert review["simulated_fills"] == {"status": "not recorded", "records": []}
    assert "order" in review["recommendations"]["classification"].lower()


def test_postmarket_reports_missing_plan_trigger_and_market_data(
    tmp_path: Path, capsys: Any
) -> None:
    plan_file = tmp_path / "plan.json"
    database = tmp_path / "signals.sqlite3"
    assert (
        main(
            [
                "postmarket",
                "--date",
                "2026-10-30",
                "--plan",
                str(plan_file),
                "--database",
                str(database),
            ],
            market_data=Data(),
            as_of=CLOSE,
        )
        == 0
    )
    review = json.loads(capsys.readouterr().out)
    assert review["plan"] is None
    assert review["triggers"] == []
    assert any("market" in item.lower() for item in review["missing_data"])


def test_postmarket_reads_snapshot_and_manual_fill_from_sqlite(
    tmp_path: Path, capsys: Any
) -> None:
    database = tmp_path / "evidence.sqlite3"
    plan_file = tmp_path / "plan.json"
    config = tmp_path / "rules.json"
    account = tmp_path / "account.json"
    config.write_text(
        '{"etfs":[{"symbol":"510300","buy_below":"3.85",'
        '"max_buy_cash":"4000","minimum_holding":100}]}',
        encoding="utf-8",
    )
    account.write_text(
        '{"as_of":"2026-10-30T10:00:00+08:00","holdings":{"510300":1200},'
        '"cost_basis":{"510300":"3.70"},"sellable":{"510300":800},'
        '"available_cash":"5000"}',
        encoding="utf-8",
    )
    assert (
        main(
            [
                "premarket",
                "--config",
                str(config),
                "--account",
                str(account),
                "--output",
                str(plan_file),
            ],
            market_data=Data(),
            as_of=MORNING,
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "account-snapshot",
                "--account",
                str(account),
                "--database",
                str(database),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "manual-fill",
                "--timestamp",
                "2026-10-30T14:30:00+08:00",
                "--symbol",
                "510300",
                "--side",
                "sell",
                "--quantity",
                "100",
                "--price",
                "3.90",
                "--database",
                str(database),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "postmarket",
                "--date",
                "2026-10-30",
                "--plan",
                str(plan_file),
                "--database",
                str(database),
            ],
            market_data=Data(),
            as_of=CLOSE,
        )
        == 0
    )
    review = json.loads(capsys.readouterr().out)
    assert review["account_snapshot"]["available_cash"] == "5000"
    assert review["account_snapshot"]["source"] == "user-entered; not broker-verified"
    assert review["manual_fills"]["records"][0]["side"] == "sell"
