"""Technical analysis for completed five-minute V1 ETF bars.

Signal scoring deliberately remains disabled until the source rule's exact
component conditions are versioned and available to this implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping

from ..market_data import MinuteBar

ZERO = Decimal(0)
BAR_INTERVAL = 5
MINIMUM_HISTORY = 180


@dataclass(frozen=True, slots=True)
class IntradayTechnicalAnalysis:
    """Inspectable per-symbol technical result; scores are not probabilities."""

    symbol: str
    trend: str | None
    indicators: dict[str, Decimal | None]
    scores: dict[str, int | None]
    candidate: str
    risk_status: str
    reasons: tuple[str, ...]
    completed_valid_bars: int


def analyze_intraday_technical(
    bars_by_symbol: Mapping[str, tuple[MinuteBar, ...]],
) -> dict[str, IntradayTechnicalAnalysis]:
    """Analyze symbols independently using only completed five-minute bars.

    Technical candidates remain WAIT until exact V1.0 score component rules are
    available. Data and anchor blockers are returned per symbol, never raised
    in a way that suppresses other symbols' analyses.
    """
    return {
        symbol: _analyze_symbol(symbol, bars)
        for symbol, bars in bars_by_symbol.items()
    }


def _analyze_symbol(
    symbol: str, supplied: tuple[MinuteBar, ...]
) -> IntradayTechnicalAnalysis:
    reasons: list[str] = []
    valid: list[MinuteBar] = []
    prior_timestamp = None
    invalid_data = False
    for bar in supplied:
        if bar.symbol != symbol or bar.interval_minutes != BAR_INTERVAL:
            invalid_data = True
            continue
        if not bar.is_completed:
            continue
        if (
            min(bar.open, bar.high, bar.low, bar.close) <= ZERO
            or bar.low > min(bar.open, bar.close)
            or bar.high < max(bar.open, bar.close)
            or bar.low > bar.high
            or bar.volume < 0
            or (bar.amount is not None and bar.amount < ZERO)
            or (prior_timestamp is not None and bar.timestamp <= prior_timestamp)
        ):
            invalid_data = True
            continue
        prior_timestamp = bar.timestamp
        valid.append(bar)
    if invalid_data:
        reasons.append("invalid or mismatched five-minute bar data")
    if len(valid) < MINIMUM_HISTORY:
        reasons.append("fewer than 180 completed valid 5-minute bars")

    indicators = _indicators(valid)
    trend = _trend(valid, indicators) if len(valid) >= MINIMUM_HISTORY else None
    if indicators["vwap_0945"] is None:
        reasons.append("missing 09:45 VWAP anchor")
    reasons.append("exact V1.0 scoring rules are unavailable; technical score and candidate are blocked")
    return IntradayTechnicalAnalysis(
        symbol=symbol,
        trend=trend,
        indicators=indicators,
        scores={"buy": None, "sell": None},
        candidate="WAIT",
        risk_status="BLOCKED",
        reasons=tuple(reasons),
        completed_valid_bars=len(valid),
    )


def _indicators(bars: list[MinuteBar]) -> dict[str, Decimal | None]:
    closes = [bar.close for bar in bars]
    ema20 = _ema(closes, 20)
    ema60 = _ema(closes, 60)
    bollinger_upper = bollinger_lower = None
    if len(closes) >= 20:
        window = closes[-20:]
        mean = sum(window, ZERO) / Decimal(20)
        deviation = (sum(((value - mean) ** 2 for value in window), ZERO) / Decimal(20)).sqrt()
        bollinger_upper = mean + Decimal(2) * deviation
        bollinger_lower = mean - Decimal(2) * deviation
    kdj_k, kdj_d, kdj_j = _kdj(bars)
    atr = _wilder_atr(bars, 14)
    vwap_anchor = _vwap_at_anchor(bars)
    return {
        "ema_20": ema20,
        "ema_60": ema60,
        "bollinger_upper": bollinger_upper,
        "bollinger_lower": bollinger_lower,
        "kdj_k": kdj_k,
        "kdj_d": kdj_d,
        "kdj_j": kdj_j,
        "atr_14": atr,
        "vwap_0945": vwap_anchor,
    }


def _ema(values: list[Decimal], period: int) -> Decimal | None:
    if len(values) < period:
        return None
    average = sum(values[:period], ZERO) / Decimal(period)
    alpha = Decimal(2) / Decimal(period + 1)
    for value in values[period:]:
        average += alpha * (value - average)
    return average


def _kdj(bars: list[MinuteBar]) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    if len(bars) < 9:
        return None, None, None
    k = d = Decimal(50)
    for end in range(8, len(bars)):
        window = bars[end - 8 : end + 1]
        low = min(bar.low for bar in window)
        high = max(bar.high for bar in window)
        rsv = Decimal(50) if high == low else (bars[end].close - low) * Decimal(100) / (high - low)
        k = (Decimal(2) * k + rsv) / Decimal(3)
        d = (Decimal(2) * d + k) / Decimal(3)
    return k, d, Decimal(3) * k - Decimal(2) * d


def _wilder_atr(bars: list[MinuteBar], period: int) -> Decimal | None:
    if len(bars) <= period:
        return None
    true_ranges = [
        max(
            bar.high - bar.low,
            abs(bar.high - bars[index - 1].close),
            abs(bar.low - bars[index - 1].close),
        )
        for index, bar in enumerate(bars)
        if index > 0
    ]
    atr = sum(true_ranges[:period], ZERO) / Decimal(period)
    for true_range in true_ranges[period:]:
        atr = (atr * Decimal(period - 1) + true_range) / Decimal(period)
    return atr


def _vwap_at_anchor(bars: list[MinuteBar]) -> Decimal | None:
    if not bars:
        return None
    session: date = bars[-1].timestamp.date()
    day_bars = [bar for bar in bars if bar.timestamp.date() == session]
    amount = volume = ZERO
    for bar in day_bars:
        if bar.timestamp.strftime("%H:%M") < "09:30":
            continue
        if bar.timestamp.strftime("%H:%M") > "09:45":
            break
        if bar.amount is None:
            return None
        amount += bar.amount
        volume += Decimal(bar.volume)
        if bar.timestamp.strftime("%H:%M") == "09:45":
            if volume == ZERO:
                return None
            return amount / volume
    return None


def _trend(
    bars: list[MinuteBar], indicators: dict[str, Decimal | None]
) -> str | None:
    ema20 = indicators["ema_20"]
    ema60 = indicators["ema_60"]
    atr = indicators["atr_14"]
    if ema20 is None or ema60 is None or atr is None or atr <= ZERO or len(bars) < 64:
        return None
    # Three-bar slope compares the latest EMA to the EMA three completed bars ago.
    previous = _ema([bar.close for bar in bars[:-3]], 20)
    if previous is None:
        return None
    d = (ema20 - ema60) / atr
    m = (ema20 - previous) / atr
    if d >= Decimal("0.90") and m >= Decimal("0.25"):
        return "STRONG_BULL"
    if d <= Decimal("-0.90") and m <= Decimal("-0.25"):
        return "STRONG_BEAR"
    if d >= Decimal("0.20") and m >= Decimal("0.05"):
        return "BULL"
    if d <= Decimal("-0.20") and m <= Decimal("-0.05"):
        return "BEAR"
    return "RANGE"
