import json
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from a500_t_trader.backtest import (
    BarSignal,
    IntradayBacktestParams,
    SignalIntent,
    StrategyRulesUnavailableError,
    TechnicalAnalysisSignalProvider,
    run_intraday_backtest,
)
from a500_t_trader.market_data import MinuteBar
from a500_t_trader.strategy.premarket import AccountSnapshot

TZ = ZoneInfo("Asia/Shanghai")


def test_ohlcv_spread_proxy_uses_half_of_prior_completed_bar_range() -> None:
    from a500_t_trader.backtest.issue85_replay import _ohlcv_spread_proxy_eligible

    tight = MinuteBar(
        "510300",
        datetime(2026, 10, 30, 9, 0, tzinfo=TZ),
        5,
        Decimal("10"),
        Decimal("10.01"),
        Decimal("9.99"),
        Decimal("10"),
        1000,
    )
    wide = MinuteBar(
        "510300",
        datetime(2026, 10, 30, 9, 5, tzinfo=TZ),
        5,
        Decimal("10"),
        Decimal("10.10"),
        Decimal("9.90"),
        Decimal("10"),
        1000,
    )

    assert _ohlcv_spread_proxy_eligible(tight, Decimal("10"), Decimal("0.001"))
    assert not _ohlcv_spread_proxy_eligible(wide, Decimal("10"), Decimal("0.001"))
    assert not _ohlcv_spread_proxy_eligible(None, Decimal("10"), Decimal("0.001"))
    assert not _ohlcv_spread_proxy_eligible(tight, Decimal("0"), Decimal("0.001"))

    incomplete = MinuteBar(
        "510300",
        tight.timestamp,
        5,
        tight.open,
        tight.high,
        tight.low,
        tight.close,
        tight.volume,
        is_completed=False,
    )
    assert not _ohlcv_spread_proxy_eligible(incomplete, Decimal("10"), Decimal("0.001"))


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


def test_event_provider_observes_prior_bar_fills_before_signaling() -> None:
    class Provider:
        observed: list[tuple[datetime, object, tuple[MinuteBar, ...], object]] = []

        def signals_at(self, symbol, history, state):
            self.observed.append(
                (history[-1].timestamp, state["holdings"], history, dict(state))
            )
            if len(history) == 1:
                return (BarSignal(history[-1].timestamp, "buy", 100),)
            return ()

    bars = (bar(30, 0, "10"), bar(30, 1, "10"), bar(30, 2, "10"))
    provider = Provider()
    result = run_intraday_backtest(
        symbol="510300",
        account=AccountSnapshot(account().as_of, {}, {}, {}, Decimal("10000")),
        bars=bars,
        signals=provider,
        implementation_version="event-provider-test",
        strategy_parameters={},
        params=IntradayBacktestParams(participation_rate=Decimal(1)),
    )

    assert [item[0] for item in provider.observed] == [item.timestamp for item in bars]
    assert [item[1] for item in provider.observed] == [0, 100, 100]
    assert all(item[2][-1].timestamp == item[0] for item in provider.observed)
    first_state = provider.observed[0][3]
    assert first_state["remaining_daily_sell_notional"] == "1000.00"
    assert first_state["remaining_daily_turnover"] == "15000.00"
    assert first_state["remaining_daily_fills"] == 8
    assert first_state["remaining_daily_additions"] == 3
    assert first_state["free_cash"] == "10000"
    assert len(result.fills) == 1
    assert (
        result.assumptions["submitted_signals"][0]["timestamp"]
        == bars[0].timestamp.isoformat()
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


def test_v1_buy_requires_explicit_spread_eligibility() -> None:
    bars = tuple(bar(30, index, "10", 1_000_000) for index in range(15))
    starting = AccountSnapshot(
        account().as_of,
        {"510300": 0},
        {"510300": Decimal("10")},
        {"510300": 0},
        Decimal("100_000"),
    )

    result = run_intraday_backtest(
        symbol="510300",
        account=starting,
        bars=bars,
        signals=(BarSignal(bars[13].timestamp, "buy", 1_000),),
        params=IntradayBacktestParams(
            participation_rate=Decimal(1),
            dynamic_sizing=True,
        ),
        strategy_parameters={},
        implementation_version="test-v1",
    )

    assert result.fills == ()
    assert result.account_states[13]["pending_orders"] == []


def test_v1_dynamic_buy_keeps_the_35_percent_nav_cash_reserve() -> None:
    bars = tuple(bar(30, index, "10", 1_000_000) for index in range(15))
    starting = AccountSnapshot(
        account().as_of,
        {"510300": 10_000},
        {"510300": Decimal("10")},
        {"510300": 10_000},
        Decimal("0"),
    )

    result = run_intraday_backtest(
        symbol="510300",
        account=starting,
        bars=bars,
        signals=(BarSignal(bars[13].timestamp, "buy", 100, spread_eligible=True),),
        params=IntradayBacktestParams(
            participation_rate=Decimal(1),
            dynamic_sizing=True,
        ),
        strategy_parameters={},
        implementation_version="test-v1",
    )

    assert result.fills == ()
    assert result.account_states[13]["pending_orders"] == []


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
            BarSignal(bars[13].timestamp, "buy", 100, spread_eligible=True),
            BarSignal(bars[13].timestamp, "buy", 100, spread_eligible=True),
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
        ("buy", 300),
    ]
    assert len(result.account_states) == len(bars)
    submitted = result.account_states[13]
    assert [item["quantity"] for item in submitted["pending_orders"]] == [1_600, 400]
    assert submitted["nav_at_close"] == "200000"
    assert result.account_states[-1]["holdings"] == 11_900
    assert result.account_states[-1]["protected_floor"] == 7_000


def test_v1_dynamic_buy_rechecks_position_capacity_at_fill_price() -> None:
    bars = tuple(
        MinuteBar(
            "510300",
            datetime(2026, 10, 30, 9, 30 + index, tzinfo=TZ),
            1,
            Decimal("10") if index < 14 else Decimal("12"),
            Decimal("10.04") if index < 14 else Decimal("12.04"),
            Decimal("9.96") if index < 14 else Decimal("11.96"),
            Decimal("10") if index < 14 else Decimal("12"),
            1_000_000,
            Decimal("10000000"),
        )
        for index in range(15)
    )
    starting = AccountSnapshot(
        account().as_of,
        {"510300": 6_000},
        {"510300": Decimal("10")},
        {"510300": 6_000},
        Decimal("40_000"),
    )

    result = run_intraday_backtest(
        symbol="510300",
        account=starting,
        bars=bars,
        signals=(BarSignal(bars[13].timestamp, "buy", 1_600, spread_eligible=True),),
        params=IntradayBacktestParams(
            participation_rate=Decimal(1),
            dynamic_sizing=True,
            protected_floor=4_000,
            fee_bps=Decimal("0.5"),
            minimum_commission=Decimal("0.1"),
        ),
        strategy_parameters={},
        implementation_version="test-v1",
    )

    assert result.account_states[-1]["holdings"] == 6_000
    assert result.fills == ()


def test_v1_dynamic_cost_grid_uses_risk_sized_order_not_one_lot() -> None:
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
        signals=(BarSignal(bars[13].timestamp, "buy", 100, spread_eligible=True),),
        params=IntradayBacktestParams(
            participation_rate=Decimal(1),
            dynamic_sizing=True,
            protected_floor=7_000,
            minimum_commission=Decimal("20"),
        ),
        strategy_parameters={},
        implementation_version="test-v1",
    )

    assert result.fills


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
            BarSignal(
                bars[14].timestamp, "buy", 3_000, "buyback", spread_eligible=True
            ),
            BarSignal(
                bars[14].timestamp, "buy", 3_000, "buyback", spread_eligible=True
            ),
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


def test_v1_pending_ordinary_sells_reserve_daily_sell_capacity() -> None:
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
        Decimal("0"),
    )
    result = run_intraday_backtest(
        symbol="510300",
        account=starting,
        bars=bars,
        signals=(
            BarSignal(bars[13].timestamp, "sell", 1_000),
            BarSignal(bars[13].timestamp, "sell", 1_000),
        ),
        params=IntradayBacktestParams(
            dynamic_sizing=True,
            protected_floor=7_000,
            order_lifetime_bars=10,
        ),
        strategy_parameters={},
        implementation_version="test-v1",
    )

    assert (
        sum(order["quantity"] for order in result.account_states[13]["pending_orders"])
        == 1_000
    )


def test_v1_partial_fills_stop_at_daily_fill_limit() -> None:
    start = datetime(2026, 10, 30, 9, 30, tzinfo=TZ)
    bars = tuple(
        MinuteBar(
            "510300",
            start + timedelta(minutes=index),
            1,
            Decimal("10"),
            Decimal("10.04"),
            Decimal("9.96"),
            Decimal("10"),
            5_000,
            Decimal("50000"),
        )
        for index in range(25)
    )
    starting = AccountSnapshot(
        account().as_of,
        {"510300": 10_000},
        {"510300": Decimal("10")},
        {"510300": 10_000},
        Decimal("0"),
    )
    result = run_intraday_backtest(
        symbol="510300",
        account=starting,
        bars=bars,
        signals=(BarSignal(bars[13].timestamp, "sell", 1_000),),
        params=IntradayBacktestParams(
            dynamic_sizing=True,
            protected_floor=7_000,
            order_lifetime_bars=20,
        ),
        strategy_parameters={},
        implementation_version="test-v1",
    )

    assert len(result.fills) == 8
    assert sum(fill.quantity for fill in result.fills) == 800
    assert result.account_states[21]["daily_fill_count"] == 8


def test_v1_daily_turnover_is_enforced_at_fill_time() -> None:
    start = datetime(2026, 10, 30, 9, 30, tzinfo=TZ)
    bars = tuple(
        MinuteBar(
            "510300",
            start + timedelta(minutes=index),
            1,
            Decimal("10"),
            Decimal("10.04"),
            Decimal("9.96"),
            Decimal("10"),
            1_000_000,
            Decimal("10000000"),
        )
        for index in range(25)
    )
    starting = AccountSnapshot(
        account().as_of,
        {"510300": 10_000},
        {"510300": Decimal("10")},
        {"510300": 10_000},
        Decimal("0"),
    )
    signals = (
        BarSignal(bars[13].timestamp, "sell", 10_000, SignalIntent.RISK_EXIT),
        *(BarSignal(bars[index].timestamp, "buy", 2_000) for index in range(14, 24)),
    )
    result = run_intraday_backtest(
        symbol="510300",
        account=starting,
        bars=bars,
        signals=signals,
        params=IntradayBacktestParams(
            dynamic_sizing=True,
            protected_floor=7_000,
            order_lifetime_bars=20,
        ),
        strategy_parameters={},
        implementation_version="test-v1",
    )

    assert Decimal(result.account_states[24]["daily_turnover"]) <= Decimal("150000")


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
