from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from a500_t_trader.__main__ import main
from a500_t_trader.market_data import MinuteBar
from a500_t_trader.trading_calendar import SessionState

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 30, 10, 1, tzinfo=SHANGHAI)


class Bars:
    def __init__(self):
        self.calls = []

    def get_intraday_bars(self, symbol, interval_minutes=1):
        self.calls.append((symbol, interval_minutes))
        return (
            MinuteBar(
                "510300",
                datetime(2026, 10, 30, 9, 0, tzinfo=SHANGHAI),
                1,
                Decimal("3.80"),
                Decimal("3.86"),
                Decimal("3.79"),
                Decimal("3.84"),
                1200,
                Decimal("4608"),
                True,
                "fixture",
            ),
            MinuteBar(
                "510300",
                datetime(2026, 10, 30, 10, 0, tzinfo=SHANGHAI),
                1,
                Decimal("3.80"),
                Decimal("3.86"),
                Decimal("3.79"),
                Decimal("3.84"),
                1200,
                Decimal("4608"),
                True,
                "fixture",
            ),
            MinuteBar(
                "510300",
                datetime(2026, 10, 30, 10, 1, tzinfo=SHANGHAI),
                1,
                Decimal("3.84"),
                Decimal("3.85"),
                Decimal("3.83"),
                Decimal("3.84"),
                500,
                Decimal("1920"),
                False,
                "fixture",
            ),
        )

    def session_status(self, session_date):
        assert session_date == NOW.date()
        return SessionState.TRADING, False


def _inputs(tmp_path):
    config = tmp_path / "strategy.json"
    account = tmp_path / "account.json"
    config.write_text(
        '{"etfs":[{"symbol":"510300","buy_below":"3.85",'
        '"sell_above":"4.10","max_buy_cash":"4000","minimum_holding":100}]}',
        encoding="utf-8",
    )
    account.write_text(
        '{"as_of":"2026-10-30T10:00:00+08:00","holdings":{"510300":1200},'
        '"cost_basis":{"510300":"3.70"},"sellable":{"510300":800},'
        '"available_cash":"5000"}',
        encoding="utf-8",
    )
    return config, account


def test_user_started_monitor_alerts_and_persists_completed_and_open_bar_evidence(
    tmp_path, capsys
):
    config, account = _inputs(tmp_path)
    database = tmp_path / "signals.sqlite3"
    data = Bars()
    command = [
        "monitor",
        "--config",
        str(config),
        "--account",
        str(account),
        "--database",
        str(database),
        "--once",
    ]

    assert main(command, market_data=data, as_of=NOW) == 0
    output = capsys.readouterr()
    assert "SUGGESTION" in output.out and "buy" in output.out
    assert "NOT AN ORDER" in output.out
    assert "expires if a later price rises above 3.85 CNY" in output.out
    assert "10:00:00+08:00" in output.out
    assert "10:01:00+08:00" in output.out
    assert "UNFINISHED BAR" in output.out
    assert "09:00:00+08:00" not in output.out
    assert data.calls == [("510300", 1)]

    import sqlite3

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT symbol, direction, condition, bar_timestamp, bar_close, "
            "bar_completed, risk_or_invalidation, account_as_of, account_source "
            "FROM trigger_evidence ORDER BY bar_timestamp"
        ).fetchall()
    assert rows == [
        (
            "510300",
            "buy",
            "low <= 3.85 CNY",
            "2026-10-30T10:00:00+08:00",
            "3.84",
            1,
            "expires if a later price rises above 3.85 CNY; user review required",
            "2026-10-30T10:00:00+08:00",
            "user-entered; not broker-verified",
        ),
        (
            "510300",
            "buy",
            "low <= 3.85 CNY",
            "2026-10-30T10:01:00+08:00",
            "3.84",
            0,
            "provisional while the minute bar is unfinished; user review required",
            "2026-10-30T10:00:00+08:00",
            "user-entered; not broker-verified",
        ),
    ]


def test_monitor_records_both_directions_when_one_bar_crosses_both_levels(
    tmp_path, capsys
):
    config, account = _inputs(tmp_path)
    config.write_text(
        '{"etfs":[{"symbol":"510300","buy_below":"3.80",'
        '"sell_above":"3.82","max_buy_cash":"4000","minimum_holding":100}]}',
        encoding="utf-8",
    )
    assert (
        main(
            [
                "monitor",
                "--config",
                str(config),
                "--account",
                str(account),
                "--database",
                str(tmp_path / "both.sqlite3"),
                "--once",
            ],
            market_data=Bars(),
            as_of=NOW,
        )
        == 0
    )
    output = capsys.readouterr().out
    assert output.count("SUGGESTION") == 3
    assert "buy" in output and "sell" in output


def test_monitor_requery_does_not_duplicate_a_trigger(tmp_path, capsys):
    config, account = _inputs(tmp_path)
    database = tmp_path / "signals.sqlite3"
    command = [
        "monitor",
        "--config",
        str(config),
        "--account",
        str(account),
        "--database",
        str(database),
        "--once",
    ]

    assert main(command, market_data=Bars(), as_of=NOW) == 0
    capsys.readouterr()
    assert main(command, market_data=Bars(), as_of=NOW) == 0
    assert "No new trigger" in capsys.readouterr().out

    import sqlite3

    with sqlite3.connect(database) as connection:
        count = connection.execute("SELECT count(*) FROM trigger_evidence").fetchone()[
            0
        ]
    assert count == 2


def test_monitor_stops_in_the_invoking_process_on_keyboard_interrupt(
    tmp_path, capsys, monkeypatch
):
    from a500_t_trader import intraday

    class LiveBars(Bars):
        def session_status(self, session_date):
            return SessionState.TRADING, False

    def stop(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(intraday.time, "sleep", stop)
    config, account = _inputs(tmp_path)
    command = [
        "monitor",
        "--config",
        str(config),
        "--account",
        str(account),
        "--database",
        str(tmp_path / "signals.sqlite3"),
    ]

    assert main(command, market_data=LiveBars()) == 0
    assert "stopped; no background process remains" in capsys.readouterr().out


def test_monitor_reports_stale_bars_without_creating_trigger_evidence(tmp_path, capsys):
    import sqlite3

    class StaleBars(Bars):
        def get_intraday_bars(self, symbol, interval_minutes=1):
            return (super().get_intraday_bars(symbol, interval_minutes)[0],)

    config, account = _inputs(tmp_path)
    database = tmp_path / "signals.sqlite3"
    command = [
        "monitor",
        "--config",
        str(config),
        "--account",
        str(account),
        "--database",
        str(database),
        "--once",
    ]

    assert main(command, market_data=StaleBars(), as_of=NOW) == 0
    result = capsys.readouterr()
    assert "stale or future-dated" in result.err
    assert "SUGGESTION" not in result.out
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT count(*) FROM trigger_evidence").fetchone()[0]
            == 0
        )
