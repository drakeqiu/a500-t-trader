import json
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from a500_t_trader.__main__ import main
from a500_t_trader.market_data import MinuteBar, Snapshot
from a500_t_trader.trading_calendar import SessionState

SHANGHAI = ZoneInfo("Asia/Shanghai")
MORNING = datetime(2026, 10, 30, 10, 0, tzinfo=SHANGHAI)
CLOSE = datetime(2026, 10, 30, 15, 0, tzinfo=SHANGHAI)


class Data:
    def get_snapshot(self, symbol):
        return Snapshot(symbol, MORNING, MORNING, last_price=Decimal("3.80"))

    def session_status(self, session_date):
        return SessionState.TRADING, False

    def get_intraday_bars(self, symbol, interval_minutes=1):
        return (
            MinuteBar(
                symbol,
                datetime(2026, 10, 30, 10, 0, tzinfo=SHANGHAI),
                1,
                Decimal("3.80"),
                Decimal("3.85"),
                Decimal("3.75"),
                Decimal("3.78"),
                1000,
                is_completed=True,
            ),
        )

    def get_history_bars(self, symbol, start_date, end_date, interval_minutes=1):
        assert start_date == end_date == date(2026, 10, 30)
        return (
            MinuteBar(
                symbol,
                datetime(2026, 10, 30, 14, 59, tzinfo=SHANGHAI),
                interval_minutes,
                Decimal("3.77"),
                Decimal("3.80"),
                Decimal("3.76"),
                Decimal("3.79"),
                2000,
                is_completed=True,
            ),
        )


def test_cli_daily_workflow_saves_plan_triggers_and_reviews_without_inventing_trades(
    tmp_path, capsys
):
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
    assert review["market_data"]["510300"]["close"] == "3.79"
    assert review["account_snapshot"]["source"] == "user-entered; not broker-verified"
    assert review["manual_fills"]["status"] == "recorded"
    assert review["manual_fills"]["records"][0]["quantity"] == 100
    assert review["manual_fills"]["records"][0]["classification"] == (
        "user-entered manual fill; not broker-verified"
    )
    assert review["simulated_fills"] == {"status": "not recorded", "records": []}
    assert "order" in review["recommendations"]["classification"].lower()


def test_postmarket_reports_missing_plan_trigger_and_market_data(tmp_path, capsys):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        json.dumps({"session_date": "2026-10-30", "plan": {"suggestions": []}})
    )
    assert (
        main(
            [
                "postmarket",
                "--date",
                "2026-10-30",
                "--plan",
                str(plan_file),
                "--database",
                str(tmp_path / "missing.sqlite3"),
            ],
            market_data=None,
            as_of=CLOSE,
        )
        == 0
    )
    review = json.loads(capsys.readouterr().out)
    assert review["missing_data"]
    assert any("trigger" in item.lower() for item in review["missing_data"])
    assert any("account snapshot" in item.lower() for item in review["missing_data"])
    assert any("market" in item.lower() for item in review["missing_data"])


def test_postmarket_reads_snapshot_and_manual_fill_from_sqlite(tmp_path, capsys):
    database = tmp_path / "evidence.sqlite3"
    account = tmp_path / "account.json"
    plan_file = tmp_path / "plan.json"
    account.write_text(
        '{"as_of":"2026-10-30T08:30:00+08:00","holdings":{"510300":100},'
        '"cost_basis":{"510300":"3.70"},"sellable":{"510300":0},'
        '"available_cash":"500"}',
        encoding="utf-8",
    )
    plan_file.write_text(
        json.dumps(
            {
                "session_date": "2026-10-30",
                "plan": {"suggestions": []},
            }
        ),
        encoding="utf-8",
    )
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
                "2026-10-30T10:02:00+08:00",
                "--symbol",
                "510300",
                "--side",
                "sell",
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
            market_data=None,
            as_of=CLOSE,
        )
        == 0
    )
    review = json.loads(capsys.readouterr().out)
    assert review["account_snapshot"]["available_cash"] == "500"
    assert review["account_snapshot"]["source"] == "user-entered; not broker-verified"
    assert review["manual_fills"]["records"][0]["side"] == "sell"
