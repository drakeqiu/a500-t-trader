from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from a500_t_trader.backtest.intraday_signals import replay_fixed_quantity_signals
from a500_t_trader.market_data import MinuteBar


def test_fixed_quantity_replay_rejects_non_lot_size() -> None:
    with pytest.raises(ValueError, match="multiple of 100"):
        replay_fixed_quantity_signals("563360", (), quantity=40001)


def test_fixed_quantity_replay_emits_no_candidate_before_warmup() -> None:
    zone = ZoneInfo("Asia/Shanghai")
    bars = tuple(
        MinuteBar(
            "563360",
            datetime(2026, 10, 30, 9, 30, tzinfo=zone)
            + timedelta(minutes=5 * index),
            5,
            Decimal("1.000"),
            Decimal("1.001"),
            Decimal("0.999"),
            Decimal("1.000"),
            10000,
            Decimal("10000"),
        )
        for index in range(179)
    )

    assert replay_fixed_quantity_signals("563360", bars) == ()
