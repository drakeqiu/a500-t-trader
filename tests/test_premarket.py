from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from _pytest.capture import CaptureFixture

from a500_t_trader.__main__ import main
from a500_t_trader.market_data import Snapshot
from a500_t_trader.premarket import AccountSnapshot, EtfRule, PlanConfig, generate_plan
from a500_t_trader.trading_calendar import SessionState

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 10, 30, 8, 30, tzinfo=SHANGHAI)


class MarketData:
    def __init__(
        self,
        snapshot: Snapshot | None,
        status: tuple[SessionState, bool] = (SessionState.TRADING, False),
    ) -> None:
        self.snapshot = snapshot
        self.status = status

    def get_snapshot(self, symbol: str) -> Snapshot | None:
        assert symbol == "510300"
        return self.snapshot

    def session_status(self, session_date: date) -> tuple[SessionState, bool]:
        assert session_date == AS_OF.date()
        return self.status


def _snapshot(
    timestamp: datetime = datetime(2026, 10, 30, 8, 20, tzinfo=SHANGHAI),
) -> Snapshot:
    return Snapshot(
        "510300",
        timestamp,
        AS_OF,
        last_price=Decimal("3.80"),
        previous_close=Decimal("3.90"),
    )


def _inputs() -> tuple[PlanConfig, AccountSnapshot]:
    config = PlanConfig(
        (
            EtfRule(
                "510300",
                buy_below=Decimal("3.85"),
                sell_above=Decimal("4.10"),
                max_buy_cash=Decimal("4000"),
                minimum_holding=100,
            ),
        )
    )
    account = AccountSnapshot(
        AS_OF,
        {"510300": 1200},
        {"510300": Decimal("3.70")},
        {"510300": 800},
        Decimal("5000"),
    )
    return config, account


def test_plan_calculates_explainable_buy_quantity_from_explicit_rule() -> None:
    config, account = _inputs()
    plan = generate_plan(config, account, MarketData(_snapshot()), as_of=AS_OF)
    suggestion = plan.suggestions[0]

    assert plan.account_source == "user-entered; not broker-verified"
    assert suggestion.direction == "buy"
    assert suggestion.quantity == 1000
    assert suggestion.condition == "price <= 3.85 CNY"
    assert (
        suggestion.reason
        == "last price 3.80 CNY meets configured buy-below rule 3.85 CNY"
    )
    assert "No signal score is defined" in suggestion.signal_strength_note
    assert suggestion.risk_or_invalidation


def test_missing_or_stale_market_data_never_recommends_tradable_quantity() -> None:
    config, account = _inputs()
    stale = _snapshot(datetime(2026, 10, 29, 8, 20, tzinfo=SHANGHAI))
    plan = generate_plan(config, account, MarketData(stale), as_of=AS_OF)

    assert "unavailable" in plan.capability_status
    assert plan.suggestions[0].quantity is None
    assert "stale" in plan.suggestions[0].risk_or_invalidation


def test_risk_control_can_veto_strategy_signal() -> None:
    config, account = _inputs()
    plan = generate_plan(
        config,
        account,
        MarketData(_snapshot(), (SessionState.TRADING, True)),
        as_of=AS_OF,
    )

    assert plan.suggestions[0].direction == "none"
    assert plan.suggestions[0].quantity is None
    assert "calendar" in plan.suggestions[0].risk_or_invalidation


def test_cli_invokes_same_plan_workflow_and_emits_advisory_json(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    config = tmp_path / "strategy.json"
    account = tmp_path / "account.json"
    config.write_text(
        '{"etfs":[{"symbol":"510300","buy_below":"3.85",'
        '"sell_above":"4.10","max_buy_cash":"4000","minimum_holding":100}]}',
        encoding="utf-8",
    )
    account.write_text(
        '{"as_of":"2026-10-30T08:30:00+08:00","holdings":{"510300":1200},'
        '"cost_basis":{"510300":"3.70"},"sellable":{"510300":800},'
        '"available_cash":"5000"}',
        encoding="utf-8",
    )
    result = main(
        ["premarket", "--config", str(config), "--account", str(account)],
        market_data=MarketData(_snapshot()),
        as_of=AS_OF,
    )
    output = capsys.readouterr()

    assert result == 0
    assert '"direction": "buy"' in output.out
    assert '"quantity": 1000' in output.out
    assert "not broker-verified" in output.out
    assert output.err == ""
