from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

from a500_t_trader.backtest.intraday import (
    BarSignal,
    IntradayBacktestParams,
    run_intraday_backtest,
)
from a500_t_trader.backtest.issue83_comparison import (
    _average_daily_amount,
    _round_trip_cost_rate,
    run_issue83_comparison,
)
from a500_t_trader.market_data import MinuteBar
from a500_t_trader.strategy.intraday_v1_config import RULE_SOURCE_SHA256
from a500_t_trader.strategy.premarket import AccountSnapshot

def test_quantity_based_round_trip_cost_uses_order_notional() -> None:
    # At ¥5, 100 shares incur two ¥0.10 minimum commissions; 10,000
    # shares incur proportional ¥2.50 fees each side plus 0.04% impact.
    assert _round_trip_cost_rate(Decimal("5"), 100) == Decimal("0.0008")
    assert _round_trip_cost_rate(Decimal("5"), 10000) == Decimal("0.0005")


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
            _bar(start + timedelta(minutes=5 * index), 5, 1)
            for index in range(24)
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
