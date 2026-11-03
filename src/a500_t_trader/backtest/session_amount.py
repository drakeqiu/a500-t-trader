"""Validation and aggregation for complete intraday session amounts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import time
from decimal import Decimal

from ..market_data import SHANGHAI, MinuteBar


def complete_session_amount(bars: Sequence[MinuteBar]) -> Decimal | None:
    """Return the amount for a complete 48-bar session, if all bars are valid."""
    local_times = {bar.timestamp.astimezone(SHANGHAI).time() for bar in bars}
    if (
        len(bars) != 48
        or len(local_times) != 48
        or max(local_times) != time(14, 55)
        or any(bar.amount is None for bar in bars)
    ):
        return None
    return sum((bar.amount for bar in bars if bar.amount is not None), Decimal(0))
