import hashlib
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from a500_t_trader.backtest.intraday import (
    BarSignal,
    IntradayBacktestParams,
    SignalIntent,
    run_intraday_backtest,
)
from a500_t_trader.backtest.issue83_comparison import (
    _average_daily_amount,
    run_issue83_comparison,
)
from a500_t_trader.backtest.issue85_replay import (
    _max_drawdown,
    _round_trip_cost_rate,
    _run_chronological_split,
    _run_issue85_sequential_replay,
    run_issue85_sequential_replay,
)
from a500_t_trader.cli.intraday import _load_completed_bars
from a500_t_trader.market_data import MinuteBar
from a500_t_trader.strategy.intraday_v1_config import RULE_SOURCE_SHA256
from a500_t_trader.strategy.premarket import AccountSnapshot


def test_quantity_based_round_trip_cost_uses_order_notional() -> None:
    # At ¥5, 100 shares incur two ¥0.10 minimum commissions; 10,000
    # shares incur proportional ¥2.50 fees each side plus 0.04% impact.
    assert _round_trip_cost_rate(Decimal("5"), 100) == Decimal("0.0008")
    assert _round_trip_cost_rate(Decimal("5"), 10000) == Decimal("0.0005")


def test_max_drawdown_handles_empty_and_nonempty_histories() -> None:
    assert _max_drawdown(()) == Decimal(0)
    states = (
        {"nav_at_close": "110"},
        {"nav_at_close": "99"},
        {"nav_at_close": "105"},
    )
    assert _max_drawdown(states, initial_cash=Decimal("100")) == Decimal("0.1")


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_comparison_groups_use_distinct_source_score_weights() -> None:
    bars = _bars()

    result = run_issue83_comparison(bars)

    groups = cast(dict[str, dict[str, object]], result["groups"])
    base_weights = cast(dict[str, int], groups["kdj15_rsi0"]["score_weights"])
    score_weights = cast(dict[str, int], groups["kdj8_rsi7"]["score_weights"])
    momentum_weights = cast(
        dict[str, int], groups["kdj8_rsi7_rsi_momentum"]["score_weights"]
    )
    momentum_group = groups["kdj8_rsi7_rsi_momentum"]
    base_group = groups["kdj8_rsi7"]
    execution = cast(dict[str, object], result["execution"])
    assert base_weights["kdj"] == 15
    assert base_weights["rsi"] == 0
    assert score_weights["kdj"] == 8
    assert score_weights["rsi"] == 7
    assert momentum_weights == score_weights
    assert momentum_group["rsi_momentum_filter"] is True
    assert (
        groups["kdj15_rsi0"]["analysis_parameter_hash"]
        != base_group["analysis_parameter_hash"]
    )
    assert cast(int, momentum_group["signal_count"]) <= cast(
        int, base_group["signal_count"]
    )
    assert execution["participation_rate"] == "0.02"
    assert result["rule_source_sha256"] == RULE_SOURCE_SHA256


def test_account_simulation_tracks_lot_cash_position_and_minimum_fee() -> None:
    bars = (
        _bar(datetime(2024, 10, 18, 14, 0), 5, 10000),
        _bar(datetime(2024, 10, 18, 14, 5), 5, 10000),
        _bar(datetime(2024, 10, 18, 14, 10), 5, 10000),
    )
    account = AccountSnapshot(
        datetime(2024, 10, 18, 13, 55, tzinfo=SHANGHAI), {}, {}, {}, Decimal(500000)
    )

    result = run_intraday_backtest(
        symbol="563360",
        account=account,
        bars=bars,
        signals=(BarSignal(bars[0].timestamp, "buy", 100),),
        params=IntradayBacktestParams(
            participation_rate=Decimal("0.02"),
            fee_bps=Decimal("0.5"),
            slippage_bps=Decimal(0),
            order_lifetime_bars=1,
            minimum_commission=Decimal("0.1"),
        ),
        strategy_parameters={"quantity": 100},
        implementation_version="issue83-test",
    )

    assert result.fills[0].quantity == 100
    assert result.fills[0].fees == Decimal("0.1")
    assert result.final_holdings == 100
    assert result.final_cash == Decimal("499499.9")
    assert result.strategy_final_value == Decimal("499999.9")


def _bars() -> tuple[MinuteBar, ...]:
    current = datetime(2024, 10, 15, 9, 30, tzinfo=SHANGHAI)
    bars = []
    while current < datetime(2024, 10, 18, 14, 15, tzinfo=SHANGHAI):
        if (
            current.time() >= time(9, 30)
            and current.time() <= time(11, 30)
            or (current.time() >= time(13, 5) and current.time() <= time(15, 0))
        ):
            bars.append(_bar(current, 5, 10000))
        current += timedelta(minutes=5)
        if current.time() == time(11, 35):
            current = current.replace(hour=13, minute=5)
        elif current.time() > time(15, 0):
            current = (current + timedelta(days=1)).replace(hour=9, minute=30)
    return tuple(bars)


def _bar(timestamp: datetime, price: int, volume: int) -> MinuteBar:
    value = Decimal(str(price))
    return MinuteBar(
        "563360",
        timestamp.replace(tzinfo=SHANGHAI) if timestamp.tzinfo is None else timestamp,
        5,
        value,
        value,
        value,
        value,
        volume,
        value * volume,
    )


def test_average_daily_turnover_requires_20_complete_amount_sessions() -> None:
    def session(day: int) -> tuple[MinuteBar, ...]:
        start = datetime(2024, 10, day, 9, 30)
        morning = tuple(
            _bar(start + timedelta(minutes=5 * index), 5, 1) for index in range(24)
        )
        afternoon_start = datetime(2024, 10, day, 13, 0)
        afternoon = tuple(
            _bar(afternoon_start + timedelta(minutes=5 * index), 5, 1)
            for index in range(24)
        )
        return (*morning, *afternoon)

    bars = tuple(bar for day in range(1, 20) for bar in session(day))
    assert _average_daily_amount(bars) is None
    completed = (*bars, *session(20))
    assert _average_daily_amount(completed) == Decimal("240")
    assert _average_daily_amount(completed[:-1]) is None


def test_issue85_short_replay_emits_sell_signal() -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "data/market-history/563360_5m_20240601_20261028.json"
    )
    bars = _load_completed_bars(str(fixture))
    result = _run_issue85_sequential_replay(
        bars,
        decision_end=datetime(2025, 1, 22, 15, tzinfo=SHANGHAI),
        include_walk_forward=False,
    )
    signals = cast(list[dict[str, object]], result["submitted_signals"])
    assert any(signal["direction"] == "sell" for signal in signals)


def test_issue85_short_chronological_split_partitions_days() -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "data/market-history/563360_5m_20240601_20261028.json"
    )
    bars = _load_completed_bars(str(fixture))
    trading_days = sorted(
        {
            bar.timestamp.astimezone(SHANGHAI).date()
            for bar in bars
            if bar.timestamp >= datetime(2024, 10, 18, tzinfo=SHANGHAI)
        }
    )[:10]
    assert len(trading_days) == 10

    result = _run_chronological_split(
        bars, trading_days, initial_cash=Decimal("500000")
    )

    assert result["training_day_count"] == 7
    assert result["test_day_count"] == 3
    assert str(result["training_end_day"]) < str(result["test_start_day"])
    training_run = cast(dict[str, object], result["training_run"])
    test_run = cast(dict[str, object], result["test_run"])
    assert int(str(training_run["decision_bar_count"])) > 0
    assert int(str(test_run["decision_bar_count"])) > 0


@pytest.mark.slow
def test_issue85_public_replay_audits_cost_across_commission_regimes() -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "data/market-history/563360_5m_20240601_20261028.json"
    )
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == (
        "2c37f69cb258699b62b5613b9513915ac4b104c116c987db6d9a48f58c5d6a25"
    )
    bars = _load_completed_bars(str(fixture))
    result = run_issue85_sequential_replay(bars)
    small_cash_result = run_issue85_sequential_replay(
        bars, run_stress_tests=False, initial_cash=Decimal("10000")
    )

    assert result["decision_bar_count"] == 23_580
    assert small_cash_result["decision_bar_count"] == 23_580
    assert result["initial_cash"] == "500000"
    assert small_cash_result["initial_cash"] == "10000"
    assert result["pair_count"] == 0
    assert result["closed_exit_trades"] > 0
    assert result["realized_exit_net_pnl"] is not None
    assert result["average_exit_net_pnl"] is not None
    submitted_signals = cast(list[dict[str, object]], result["submitted_signals"])
    assert any(signal["direction"] == "sell" for signal in submitted_signals)
    chronological_split = cast(dict[str, object], result["chronological_split"])
    assert chronological_split["method"] == "chronological 70/30 trading-day split"
    assert chronological_split["training_day_count"] == 344
    assert chronological_split["test_day_count"] == 148
    assert str(chronological_split["training_end_day"]) < str(
        chronological_split["test_start_day"]
    )
    training_gate = cast(dict[str, object], chronological_split["training_gate"])
    test_gate = cast(dict[str, object], chronological_split["test_gate"])
    assert training_gate["status"] in {"passed", "failed", "undefined"}
    assert test_gate["status"] in {"passed", "failed", "undefined"}
    for replay in (result, small_cash_result):
        initial_cash = Decimal(str(replay["initial_cash"]))
        final_nav = Decimal(str(replay["final_nav"]))
        net_pnl = Decimal(str(replay["net_pnl"]))
        assert net_pnl == final_nav - initial_cash
        assert Decimal(str(replay["return"])) == net_pnl / initial_cash
    walk_forward = result["walk_forward"]
    assert isinstance(walk_forward, dict)
    folds = walk_forward["windows"]
    assert isinstance(folds, list) and len(folds) == 5
    assert walk_forward["assessed"] is all(
        fold["test_gate"]["assessed"] for fold in folds
    )
    assert walk_forward["passed"] is (
        sum(fold["test_gate"]["passed"] for fold in folds) >= 4
        and sum(fold["test_gate"]["assessed"] for fold in folds) >= 4
    )
    for index, fold in enumerate(folds):
        assert fold["training_start_day"] == "2024-10-18"
        assert fold["training_end_day"] < fold["test_start_day"]
        if index:
            assert folds[index - 1]["training_end_day"] < fold["training_end_day"]
            assert folds[index - 1]["test_end_day"] < fold["test_end_day"]
        assert fold["training_run"]["decision_bar_count"] > 0
        training_gate = fold["training_gate"]
        training_run = fold["training_run"]
        if training_run["closed_pairs"] == 0 or training_run["profit_factor"] is None:
            assert training_gate["status"] == "undefined"
            assert training_gate["passed"] is False
        else:
            assert training_gate["status"] in {"passed", "failed"}
        assert fold["test_run"]["decision_bar_count"] > 0
        test_gate = fold["test_gate"]
        test_run = fold["test_run"]
        if test_run["closed_pairs"] == 0 or test_run["profit_factor"] is None:
            assert test_gate["status"] == "undefined"
            assert test_gate["passed"] is False
            assert test_gate["profit_factor"] is None
        else:
            expected_pass = (
                Decimal(str(test_run["paired_realized_net_pnl"])) > 0
                and Decimal(str(test_run["profit_factor"])) > 1
            )
            assert test_gate["passed"] is expected_pass
            assert test_gate["status"] == ("passed" if expected_pass else "failed")
    inputs = result["inputs"]
    assert inputs == {
        "fee_bps": "0.5",
        "minimum_commission": "0.1",
        "slippage_bps": "0",
        "impact_bps": "2.0000",
        "cost_multiplier": "1",
    }
    daily_reset_examples = result["daily_reset_examples"]
    assert isinstance(daily_reset_examples, list) and daily_reset_examples
    first_reset = daily_reset_examples[0]
    states = result["account_states"]
    assert isinstance(states, list)
    assert len(states) == result["decision_bar_count"]
    prior_state = next(
        state
        for state in reversed(states)
        if state["timestamp"][:10] < first_reset["date"]
    )
    assert first_reset["daily_start_nav"] == prior_state["nav_at_close"]
    assert first_reset["previous_close_nav"] == prior_state["nav_at_close"]
    first_nav = Decimal(str(first_reset["first_decision_nav"]))
    previous_nav = Decimal(str(first_reset["previous_close_nav"]))
    assert Decimal(str(first_reset["first_decision_daily_return"])) == (
        (first_nav - previous_nav) / previous_nav
    )
    assumptions = result["assumptions"]
    assert isinstance(assumptions, dict)
    assert assumptions["fee"] == {
        "value": "0.5",
        "unit": "bps per side",
        "source": "user-provided commission rate, recorded in "
        "docs/backtest-563360-v1.md",
        "context": "applied independently to each simulated fill",
        "observed": False,
    }
    assert assumptions["minimum_commission"] == {
        "value": "0.1",
        "unit": "CNY per fill",
        "source": "user-provided minimum commission, recorded in "
        "docs/backtest-563360-v1.md",
        "context": "applied independently to each simulated fill",
        "observed": False,
    }
    assert assumptions["slippage"] == {
        "value": "0",
        "unit": "bps per side",
        "source": "exploratory baseline assumption recorded in "
        "docs/backtest-563360-v1.md",
        "context": "idealized next-available-bar-open baseline; not observed "
        "fill evidence",
        "observed": False,
    }
    assert assumptions["impact"] == {
        "value": "2.0000",
        "unit": "bps per side",
        "source": "modeled cost-grid assumption in the Issue 85 replay",
        "context": "used to estimate candidate cost only; not applied again to "
        "simulated fill prices",
        "observed": False,
    }
    assert assumptions["cost_multiplier"] == {
        "value": "1",
        "unit": "x",
        "source": "registered baseline or stress replay configuration",
        "context": "multiplies modeled fees, minimum commission, slippage, and "
        "impact inputs",
    }
    assert assumptions["zero_slippage_assumption"] == {
        "bps_per_side": "0",
        "source": "exploratory baseline assumption recorded in "
        "docs/backtest-563360-v1.md",
        "recorded_on": "2026-11-03",
        "context": "idealized next-available-bar-open baseline; not observed "
        "fill or market-impact evidence",
        "observed": False,
    }
    stress_tests = result["stress_tests"]
    assert isinstance(stress_tests, dict)
    cost_stress = stress_tests["cost_1_5x"]
    assert cost_stress["cost_multiplier"] == "1.5"
    assert cost_stress["inputs"] == {
        "fee_bps": "0.75",
        "minimum_commission": "0.15",
        "slippage_bps": "0.0",
        "impact_bps": "3.00000",
    }
    liquidity_examples = result["liquidity_quantity_examples"]
    assert isinstance(liquidity_examples, list) and liquidity_examples
    for example in liquidity_examples:
        quantity = int(example["quantity"])
        volume = int(example["decision_bar_volume"])
        assert quantity <= volume * 2 // 100
        assert quantity % 100 == 0
    crossover_notional = Decimal("0.1") / Decimal("0.00005")

    def eligible_buy_examples(replay: dict[str, object]) -> list[dict[str, object]]:
        examples = replay["cost_grid_examples"]
        assert isinstance(examples, list)
        buys = [
            example
            for example in examples
            if example["candidate"] == "BUY" and example["eligible"] is True
        ]
        assert buys
        for example in buys:
            quantity = int(example["quantity"])
            price = Decimal(str(example["price"]))
            notional = price * quantity
            expected = Decimal("2") * max(
                Decimal("0.1"), notional * Decimal("0.00005")
            ) / notional + Decimal("0.0004")
            assert quantity >= 100 and quantity % 100 == 0
            assert Decimal(str(example["quantity_cost_rate"])) == expected
            assert Decimal(str(example["grid_step"])) > 0
        return buys

    baseline_buys = eligible_buy_examples(result)
    small_cash_buys = eligible_buy_examples(small_cash_result)
    assert len({example["quantity"] for example in baseline_buys}) >= 2
    proportional_examples = [
        example
        for example in baseline_buys
        if Decimal(str(example["price"])) * int(str(example["quantity"]))
        >= crossover_notional
    ]
    assert proportional_examples
    for example in proportional_examples:
        assert Decimal(str(example["quantity_cost_rate"])) == Decimal("0.00050")

    minimum_commission_examples = [
        example
        for example in small_cash_buys
        if Decimal(str(example["price"])) * int(str(example["quantity"]))
        < crossover_notional
    ]
    assert minimum_commission_examples
    for example in minimum_commission_examples:
        assert Decimal(str(example["quantity_cost_rate"])) > Decimal("0.00050")


def test_risk_exit_realized_pnl_is_reported_separately_from_pairs() -> None:
    from a500_t_trader.backtest.intraday import IntradayFill
    from a500_t_trader.backtest.issue85_replay import _realized_exit_metrics

    timestamp = datetime(2026, 1, 2, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    fills = (
        IntradayFill(
            "buy",
            100,
            timestamp,
            timestamp,
            Decimal("10"),
            Decimal("0.1"),
            Decimal(0),
            SignalIntent.ORDINARY,
        ),
        IntradayFill(
            "sell",
            100,
            timestamp,
            timestamp,
            Decimal("11"),
            Decimal("0.1"),
            Decimal(0),
            SignalIntent.RISK_EXIT,
        ),
    )

    assert _realized_exit_metrics(
        fills, initial_quantity=0, initial_cost=Decimal(0)
    ) == {
        "closed_exit_trades": 1,
        "realized_exit_net_pnl": "99.800",
        "average_exit_net_pnl": "99.800",
        "exit_profit_factor": None,
    }
