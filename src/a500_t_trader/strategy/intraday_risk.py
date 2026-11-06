"""Shared V1 risk calculations for execution and historical simulation."""

from decimal import Decimal

_ATR_STOP_MULTIPLIER = Decimal("1.5")
_GRID_STOP_MULTIPLIER = Decimal("1.2")
_MINIMUM_STOP_PRICE_RATIO = Decimal("0.004")


def v1_stop_distance(atr: Decimal, grid_step: Decimal, price: Decimal) -> Decimal:
    """Return the V1 risk distance without changing caller-specific sizing rules."""
    return max(
        _ATR_STOP_MULTIPLIER * atr,
        _GRID_STOP_MULTIPLIER * grid_step,
        _MINIMUM_STOP_PRICE_RATIO * price,
    )
