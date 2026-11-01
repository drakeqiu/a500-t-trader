from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from a500_t_trader.__main__ import main
from a500_t_trader.market_data import Snapshot
from a500_t_trader.trading_calendar import SessionState

NOW = datetime(2026, 10, 30, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


class Data:
    def __init__(self, *, stale=False, calendar_stale=False):
        timestamp = datetime(2026, 10, 29, 8, 20, tzinfo=NOW.tzinfo) if stale else NOW
        self.snapshot = Snapshot("510300", timestamp, NOW, last_price=Decimal("3.80"), previous_close=Decimal("3.90"))
        self.calendar_stale = calendar_stale

    def get_snapshot(self, symbol: str):
        return self.snapshot

    def session_status(self, session_date: date):
        return SessionState.TRADING, self.calendar_stale


def test_cli_workflow_emits_plan_then_veto_and_missing_input(tmp_path, capsys):
    config = tmp_path / "rules.json"
    account = tmp_path / "account.json"
    config.write_text(
        '{"etfs":[{"symbol":"510300","buy_below":"3.85",'
        '"max_buy_cash":"4000","minimum_holding":100}]}', encoding="utf-8"
    )
    account.write_text(
        '{"as_of":"2026-10-30T08:30:00+08:00","holdings":{"510300":1200},'
        '"cost_basis":{"510300":"3.70"},"sellable":{"510300":800},'
        '"available_cash":"5000"}', encoding="utf-8"
    )
    command = ["premarket", "--config", str(config), "--account", str(account)]

    assert main(command, market_data=Data(), as_of=NOW) == 0
    planned = capsys.readouterr().out
    assert '"direction": "buy"' in planned
    assert '"quantity": 1000' in planned

    assert main(command, market_data=Data(calendar_stale=True), as_of=NOW) == 0
    vetoed = capsys.readouterr().out
    assert '"direction": "none"' in vetoed
    assert '"quantity": null' in vetoed
    assert "calendar" in vetoed

    assert main(command, market_data=Data(stale=True), as_of=NOW) == 0
    unavailable = capsys.readouterr().out
    assert "unavailable:" in unavailable
    assert '"quantity": null' in unavailable
    assert "stale market data" in unavailable
