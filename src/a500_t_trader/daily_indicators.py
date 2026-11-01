"""Deterministic rolling indicators over provider-returned daily bars."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .market_data import DailyBar


@dataclass(frozen=True, slots=True)
class DailyIndicators:
    bar: DailyBar
    sma_5: Decimal | None
    sma_20: Decimal | None
    rsi_14: Decimal | None


def calculate_indicators(bars: tuple[DailyBar, ...]) -> tuple[DailyIndicators, ...]:
    """Calculate close-based SMA(5), SMA(20), and Wilder RSI(14).

    Warm-up values are None until the complete observation window exists. Every
    provider-returned bar is an observation; weekends/holidays are not filled.
    """
    previous = None
    closes: list[Decimal] = []
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    average_gain: Decimal | None = None
    average_loss: Decimal | None = None
    rows: list[DailyIndicators] = []
    for bar in bars:
        if previous is not None and bar.timestamp <= previous:
            raise ValueError("daily bars must be unique and ordered ascending")
        previous = bar.timestamp
        closes.append(bar.close)
        delta = None if len(closes) == 1 else closes[-1] - closes[-2]
        rsi: Decimal | None = None
        if delta is not None:
            gains.append(max(delta, Decimal(0)))
            losses.append(max(-delta, Decimal(0)))
            if len(gains) == 14:
                average_gain = sum(gains, Decimal(0)) / Decimal(14)
                average_loss = sum(losses, Decimal(0)) / Decimal(14)
            elif len(gains) > 14:
                average_gain = (average_gain * 13 + gains[-1]) / Decimal(14)
                average_loss = (average_loss * 13 + losses[-1]) / Decimal(14)
            if average_gain is not None and average_loss is not None:
                if average_gain == 0 and average_loss == 0:
                    rsi = Decimal(50)
                elif average_loss == 0:
                    rsi = Decimal(100)
                else:
                    rsi = Decimal(100) - Decimal(100) / (
                        Decimal(1) + average_gain / average_loss
                    )
        sma_5 = sum(closes[-5:], Decimal(0)) / 5 if len(closes) >= 5 else None
        sma_20 = sum(closes[-20:], Decimal(0)) / 20 if len(closes) >= 20 else None
        rows.append(DailyIndicators(bar, sma_5, sma_20, rsi))
    return tuple(rows)
