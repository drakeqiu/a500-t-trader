"""Fixed-size, signal-only replay of V1 technical candidates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from ..market_data import MinuteBar
from ..strategy.intraday_technical import IntradayTechnicalAnalysis
from .intraday import BarSignal, TechnicalAnalysisSignalProvider

BASIS_POINTS = Decimal(10000)
DEFAULT_QUANTITY = 40000
DEFAULT_TICK_SIZE = Decimal("0.001")
DEFAULT_COMMISSION_BPS = Decimal("0.5")
DEFAULT_MINIMUM_COMMISSION = Decimal("0.1")


@dataclass(frozen=True, slots=True)
class _FixedQuantity:
    quantity: int

    def quantity_for(
        self,
        symbol: str,
        analysis: IntradayTechnicalAnalysis,
        bars: tuple[MinuteBar, ...],
    ) -> int:
        del symbol, analysis, bars
        return self.quantity


@dataclass(frozen=True, slots=True)
class _GridContext:
    quantity: int
    commission_bps: Decimal
    minimum_commission: Decimal

    def context_for(
        self, symbol: str, bars: tuple[MinuteBar, ...]
    ) -> Mapping[str, object]:
        del symbol
        notional = bars[-1].close * self.quantity
        one_way_commission = max(
            notional * self.commission_bps / BASIS_POINTS,
            self.minimum_commission,
        )
        return {
            "tick_size": DEFAULT_TICK_SIZE,
            "round_trip_cost_rate": 2 * one_way_commission / notional,
        }


def replay_fixed_quantity_signals(
    symbol: str,
    bars: tuple[MinuteBar, ...],
    *,
    quantity: int = DEFAULT_QUANTITY,
) -> tuple[BarSignal, ...]:
    """Return V1 BUY/SELL technical candidates with a fixed board-lot size."""
    if quantity <= 0 or quantity % 100:
        raise ValueError("quantity must be a positive multiple of 100")
    provider = TechnicalAnalysisSignalProvider(
        quantity_for=_FixedQuantity(quantity),
        grid_context_for=_GridContext(
            quantity, DEFAULT_COMMISSION_BPS, DEFAULT_MINIMUM_COMMISSION
        ),
        max_history_bars=181,
    )
    return provider.signals(symbol, bars)
