from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from a500_t_trader.market_data import Snapshot
from a500_t_trader.market_data.trading_calendar import SessionState
from a500_t_trader.strategy.premarket import (
    AccountSnapshot,
    EtfRule,
    PlanConfig,
    generate_plan,
)

NOW = datetime(2026, 10, 30, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


class Data:
    def get_snapshot(self, symbol: str) -> Snapshot:
        return Snapshot(symbol, NOW, NOW, last_price=Decimal("4.20"))

    def session_status(self, session_date: date) -> tuple[SessionState, bool]:
        return SessionState.TRADING, False


def test_sell_quantity_respects_user_sellable_shares_and_minimum_holding() -> None:
    plan = generate_plan(
        PlanConfig((EtfRule("510300", None, Decimal("4.10"), Decimal(0), 500),)),
        AccountSnapshot(
            NOW,
            {"510300": 1200},
            {"510300": Decimal("3.70")},
            {"510300": 600},
            Decimal("1000"),
        ),
        Data(),
        as_of=NOW,
    )

    suggestion = plan.suggestions[0]
    assert suggestion.direction == "sell"
    assert suggestion.quantity == 600
    assert "minimum holding" in suggestion.risk_or_invalidation


def test_missing_account_position_fields_veto_tradable_quantity() -> None:
    plan = generate_plan(
        PlanConfig((EtfRule("510300", Decimal("4.30"), None, Decimal("1000"), 0),)),
        AccountSnapshot(NOW, {}, {}, {}, Decimal("1000")),
        Data(),
        as_of=NOW,
    )

    assert plan.suggestions[0].direction == "none"
    assert plan.suggestions[0].quantity is None
    assert "account fields unavailable" in plan.suggestions[0].risk_or_invalidation
    assert "unavailable" in plan.capability_status
