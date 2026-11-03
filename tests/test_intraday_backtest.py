import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from a500_t_trader.backtest import (
    BarSignal,
    IntradayBacktestParams,
    StrategyRulesUnavailableError,
    TechnicalAnalysisSignalProvider,
    run_intraday_backtest,
)
from a500_t_trader.market_data import MinuteBar
from a500_t_trader.strategy.premarket import AccountSnapshot

TZ = ZoneInfo("Asia/Shanghai")


def bar(day: int, minute: int, price: str, volume: int = 1000) -> MinuteBar:
    value = Decimal(price)
    return MinuteBar(
        "510300",
        datetime(2026, 10, day, 9, minute, tzinfo=TZ),
        1,
        value,
        value,
        value,
        value,
        volume,
    )


def account() -> AccountSnapshot:
    return AccountSnapshot(
        datetime(2026, 10, 29, 15, tzinfo=TZ),
        {"510300": 200},
        {"510300": Decimal("10")},
        {"510300": 100},
        Decimal("10000"),
    )


def test_partial_fills_costs_t1_and_hold_baseline() -> None:
    bars = (
        bar(30, 0, "10"),
        bar(30, 1, "10", 250),
        bar(30, 2, "10", 250),
        bar(31, 0, "12"),
    )
    result = run_intraday_backtest(
        symbol="510300",
        account=account(),
        bars=bars,
        signals=(BarSignal(bars[0].timestamp, "buy", 200),),
        params=IntradayBacktestParams(
            participation_rate=Decimal("0.5"),
            fee_bps=Decimal("0.5"),
            slippage_bps=Decimal("10"),
            order_lifetime_bars=2,
            minimum_commission=Decimal("0.1"),
        ),
        strategy_parameters={"profile": "source-rules-v1"},
        implementation_version="test-v1",
    )

    assert [fill.fill_timestamp for fill in result.fills] == [
        bars[1].timestamp,
        bars[2].timestamp,
    ]
    assert [fill.fees for fill in result.fills] == [
        Decimal("0.1"),
        Decimal("0.1"),
    ]
    assert [fill.quantity for fill in result.fills] == [100, 100]
    assert all(fill.signal_timestamp == bars[0].timestamp for fill in result.fills)
    assert result.final_holdings == 400
    assert result.baseline_final_value == Decimal("12400")
    assert result.snapshot_bar_count == 4
    assert result.strategy_parameters == {"profile": "source-rules-v1"}
    assert result.assumptions["fill_timing"] == "next_available_bar_open"
    assert result.assumptions["minimum_commission"] == Decimal("0.1")
    assert json.loads(result.snapshot_json)[0]["close"] == "10"
    initial_snapshot = result.assumptions["initial_account_snapshot"]
    assert isinstance(initial_snapshot, dict)
    assert initial_snapshot["cost_basis"] == {"510300": "10"}


def test_public_simulation_fails_when_v1_rules_are_not_available() -> None:
    with pytest.raises(StrategyRulesUnavailableError, match="signal provider"):
        run_intraday_backtest(
            symbol="510300",
            account=account(),
            bars=(bar(30, 0, "10"), bar(30, 1, "10")),
            signals=None,
            params=IntradayBacktestParams(),
            strategy_parameters={},
            implementation_version="test-v1",
        )


def test_public_simulation_cancels_unfilled_remainder_and_releases_reserve() -> None:
    bars = (bar(30, 0, "10"), bar(30, 1, "10", 250), bar(30, 2, "10", 250))
    result = run_intraday_backtest(
        symbol="510300",
        account=account(),
        bars=bars,
        signals=(BarSignal(bars[0].timestamp, "buy", 200),),
        params=IntradayBacktestParams(
            participation_rate=Decimal("0.5"),
            order_lifetime_bars=1,
        ),
        strategy_parameters={},
        implementation_version="test-v1",
    )

    assert [fill.quantity for fill in result.fills] == [100]
    assert result.cancellations[0]["unfilled_quantity"] == 100
    assert result.cancellations[0]["reserved_cash_released"] == Decimal("1000.0")


def test_public_simulation_enforces_t_plus_one_for_new_inventory() -> None:
    bars = (bar(30, 0, "10"), bar(30, 1, "10"), bar(31, 0, "10"), bar(31, 1, "10"))
    empty_account = AccountSnapshot(account().as_of, {}, {}, {}, Decimal("10000"))
    result = run_intraday_backtest(
        symbol="510300",
        account=empty_account,
        bars=bars,
        signals=(
            BarSignal(bars[0].timestamp, "buy", 100),
            BarSignal(bars[1].timestamp, "sell", 100),
            BarSignal(bars[2].timestamp, "sell", 100),
        ),
        params=IntradayBacktestParams(participation_rate=Decimal(1)),
        strategy_parameters={},
        implementation_version="test-v1",
    )

    assert [(fill.side, fill.fill_timestamp) for fill in result.fills] == [
        ("buy", bars[1].timestamp),
        ("sell", bars[3].timestamp),
    ]
    assert result.final_holdings == 0
    
    
def test_v1_dynamic_sizing_fails_closed_without_risk_history() -> None:
    bars = (bar(30, 0, "10"), bar(30, 1, "10"))
    starting = AccountSnapshot(
        account().as_of,
        {"510300": 10_000},
        {"510300": Decimal("10")},
        {"510300": 10_000},
        Decimal("100_000"),
    )
    result = run_intraday_backtest(
        symbol="510300",
        account=starting,
        bars=bars,
        signals=(BarSignal(bars[0].timestamp, "buy", 5_000),),
        params=IntradayBacktestParams(
            participation_rate=Decimal(1),
            dynamic_sizing=True,
            protected_floor=7_000,
        ),
        strategy_parameters={},
        implementation_version="test-v1",
    )

    assert result.fills == ()
    assert result.final_holdings == 10_000


def test_v1_dynamic_sizing_uses_risk_capacity_and_reports_each_bar_state() -> None:
    bars = tuple(
        MinuteBar(
            "510300",
            datetime(2026, 10, 30, 9, 30 + index, tzinfo=TZ),
            1,
            Decimal("10"),
            Decimal("10.04"),
            Decimal("9.96"),
            Decimal("10"),
            1_000_000,
            Decimal("10000000"),
        )
        for index in range(15)
    )
    starting = AccountSnapshot(
        account().as_of,
        {"510300": 10_000},
        {"510300": Decimal("10")},
        {"510300": 10_000},
        Decimal("100_000"),
    )
    result = run_intraday_backtest(
        symbol="510300",
        account=starting,
        bars=bars,
        signals=(
            BarSignal(bars[13].timestamp, "buy", 100),
            BarSignal(bars[13].timestamp, "buy", 100),
        ),
        params=IntradayBacktestParams(
            participation_rate=Decimal(1),
            dynamic_sizing=True,
            protected_floor=7_000,
            fee_bps=Decimal("0.5"),
            minimum_commission=Decimal("0.1"),
        ),
        strategy_parameters={"sizing": "v1"},
        implementation_version="test-v1",
    )

    assert [(fill.side, fill.quantity) for fill in result.fills] == [
        ("buy", 1_600),
        ("buy", 400),
    ]
    assert len(result.account_states) == len(bars)
    submitted = result.account_states[13]
    assert [item["quantity"] for item in submitted["pending_orders"]] == [1_600, 400]
    assert submitted["nav_at_close"] == "200000"
    assert result.account_states[-1]["holdings"] == 12_000
    assert result.account_states[-1]["protected_floor"] == 7_000


def test_v1_pair_ledger_closes_only_paired_buyback_quantity() -> None:
    bars = tuple(
        MinuteBar(
            "510300",
            datetime(2026, 10, 30, 9, 30 + index, tzinfo=TZ),
            1,
            Decimal("10"),
            Decimal("10.04"),
            Decimal("9.96"),
            Decimal("10"),
            1_000_000,
            Decimal("10000000"),
        )
        for index in range(16)
    )
    starting = AccountSnapshot(
        account().as_of,
        {"510300": 10_000},
        {"510300": Decimal("10")},
        {"510300": 10_000},
        Decimal("200_000"),
    )
    result = run_intraday_backtest(
        symbol="510300",
        account=starting,
        bars=bars,
        signals=(
            BarSignal(bars[13].timestamp, "sell", 3_000),
            BarSignal(bars[14].timestamp, "buy", 3_000, "buyback"),
            BarSignal(bars[14].timestamp, "buy", 3_000, "buyback"),
        ),
        params=IntradayBacktestParams(
            participation_rate=Decimal(1),
            dynamic_sizing=True,
            protected_floor=7_000,
        ),
        strategy_parameters={},
        implementation_version="test-v1",
    )

    assert [(fill.side, fill.quantity) for fill in result.fills] == [
        ("sell", 3_000),
        ("buy", 2_400),
        ("buy", 600),
    ]
    assert result.pair_ledger[0]["status"] == "closed"
    assert result.pair_ledger[0]["remaining_quantity"] == 0
    assert result.pair_ledger[0]["buyback_quantity"] == 3_000

def test_v1_dynamic_sizing_does_not_sell_protected_inventory() -> None:
    bars = (bar(30, 0, "10", 100_000), bar(30, 1, "10", 100_000))
    starting = AccountSnapshot(
        account().as_of,
        {"510300": 10_000},
        {"510300": Decimal("10")},
        {"510300": 10_000},
        Decimal("100_000"),
    )
    result = run_intraday_backtest(
        symbol="510300",
        account=starting,
        bars=bars,
        signals=(BarSignal(bars[0].timestamp, "sell", 5_000),),
        params=IntradayBacktestParams(
            participation_rate=Decimal(1),
            dynamic_sizing=True,
            protected_floor=7_000,
        ),
        strategy_parameters={},
        implementation_version="test-v1",
    )

    assert result.fills[0].quantity == 2_000
    assert result.final_holdings == 8_000


def test_public_simulation_accepts_strategy_signal_provider() -> None:
    bars = (bar(30, 0, "10"), bar(30, 1, "10"))

    class Provider:
        def signals(
            self, symbol: str, supplied_bars: tuple[MinuteBar, ...]
        ) -> tuple[BarSignal, ...]:
            assert symbol == "510300"
            assert supplied_bars == bars
            return (BarSignal(bars[0].timestamp, "buy", 100),)

    result = run_intraday_backtest(
        symbol="510300",
        account=account(),
        bars=bars,
        signals=Provider(),
        params=IntradayBacktestParams(),
        strategy_parameters={},
        implementation_version="test-v1",
    )

    assert result.fills[0].signal_timestamp == bars[0].timestamp
    assert result.fills[0].fill_timestamp == bars[1].timestamp


def test_technical_provider_fails_closed_without_sufficient_history() -> None:
    bars = tuple(
        MinuteBar(
            "510300",
            datetime(2026, 10, 30, 9, 30 + 5 * index, tzinfo=TZ),
            5,
            Decimal("4"),
            Decimal("4.01"),
            Decimal("3.99"),
            Decimal("4"),
            10000,
            Decimal("40000"),
        )
        for index in range(2)
    )

    class QuantityResolver:
        def quantity_for(
            self, symbol: str, analysis: object, history: tuple[MinuteBar, ...]
        ) -> int:
            pytest.fail("blocked technical candidates must not reach quantity sizing")

    provider = TechnicalAnalysisSignalProvider(quantity_for=QuantityResolver())

    assert provider.signals("510300", bars) == ()
