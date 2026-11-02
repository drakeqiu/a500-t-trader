"""V1.0 technical analysis for completed five-minute ETF bars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json
from typing import Mapping

from ..market_data import MinuteBar, SHANGHAI
from .intraday_v1_config import IntradayV1Config
RULE_SOURCE_SHA256 = "6c71ca0892a76f4c479454107ace005bbdec2dd3f96d823727b44ae0e01c1689"

ZERO = Decimal(0)
ONE = Decimal(1)
BAR_INTERVAL = 5
MINIMUM_HISTORY = 180
TECHNICAL_PARAMETER_VERSION = "intraday-etf-t-v1"
TECHNICAL_PARAMETERS = {
    "indicators": {"ema": [20, 60], "bollinger": [20, 2], "kdj": [9, 3, 3], "atr": 14},
    "trend": {"bull_d": "0.20", "bull_m": "0.05", "strong_d": "0.90", "strong_m": "0.25"},
    "scores": {
        "weights": {"ema": 20, "bollinger": 20, "kdj": 15, "vwap": 25, "atr_grid": 20},
        "bollinger_z_start": "0.35", "bollinger_z_full": "1.00",
        "vwap_atr_start": "0.30", "vwap_atr_full": "1.20",
        "direction_delta": 12, "buy": 72, "sell": 68, "take_profit": 60, "buyback": 68,
        "kdj_cross_buy_min": 35, "kdj_cross_sell_min": 65,
    },
    "grid": {
        "atr_multiplier": "0.9", "strong_trend_multiplier": "1.20",
        "minimum_ratio": "0.0025", "maximum_ratio": "0.009",
        "minimum_ticks": 4, "levels": 3, "anchor": "09:45",
        "reclaim_score_proximity": "0.1",
    },
    "timing": {
        "buy_sell_windows": [["09:50", "11:20"], ["13:10", "14:30"]],
        "no_buy_after": "14:30", "closeout_only_after": "14:45",
        "protection_only_after": "14:50",
    },
}
TECHNICAL_PARAMETER_HASH = hashlib.sha256(
    json.dumps(TECHNICAL_PARAMETERS, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


@dataclass(frozen=True, slots=True)
class IntradayTechnicalAnalysis:
    """Inspectable per-symbol technical result; scores are rule-match scores, not probabilities."""

    symbol: str
    trend: str | None
    indicators: dict[str, object]
    scores: dict[str, float | None]
    candidate: str
    risk_status: str
    reasons: tuple[str, ...]
    completed_valid_bars: int
    score_breakdown: dict[str, float] | None = None
    grid_step: Decimal | None = None
    parameter_version: str = TECHNICAL_PARAMETER_VERSION
    parameter_hash: str = TECHNICAL_PARAMETER_HASH


def analyze_intraday_technical(
    bars_by_symbol: Mapping[str, tuple[MinuteBar, ...]],
    *,
    config: IntradayV1Config | None = None,
    grid_context: Mapping[str, Mapping[str, object]] | None = None,
    as_of: datetime | None = None,
) -> dict[str, IntradayTechnicalAnalysis]:
    """Score each symbol independently using tracked V1 rules.

    ``grid_context[symbol]`` requires ``tick_size`` and quantity-specific
    ``round_trip_cost_rate``. Optional ``grid_date``, ``current_step``,
    ``pending_grid_orders``, and ``used_levels`` preserve grid state across calls;
    persist the returned ``grid_step`` for the session. A supplied V1 config is
    validated against the tracked source and its fingerprint is returned as
    ``parameter_hash``. Without a config, results carry the hash of the explicit
    built-in technical-rule parameters. Candidates are technical-only; holdings,
    legal sellability, and other execution eligibility remain downstream.
    """
    if config is not None and (
        config.strategy_id != TECHNICAL_PARAMETER_VERSION
        or config.rule_version != "V1.0"
        or config.rule_source_sha256.lower() != RULE_SOURCE_SHA256
    ):
        raise ValueError("technical analysis requires the tracked intraday ETF T V1.0 configuration")
    parameter_hash = TECHNICAL_PARAMETER_HASH if config is None else config.fingerprint
    return {
        symbol: _analyze_symbol(symbol, bars, (grid_context or {}).get(symbol), as_of, parameter_hash)
        for symbol, bars in bars_by_symbol.items()
    }


def _analyze_symbol(
    symbol: str,
    supplied: tuple[MinuteBar, ...],
    grid_context: Mapping[str, object] | None,
    as_of: datetime | None,
    parameter_hash: str,
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
    enough_history = len(valid) >= MINIMUM_HISTORY
    trend = _trend(valid, indicators) if enough_history else None
    if indicators["vwap_0945"] is None:
        reasons.append("missing 09:45 VWAP anchor")
    breakdown = _score_components(valid, indicators, trend) if enough_history else None
    grid_step = None
    if enough_history and breakdown is not None:
        grid = _grid(valid, indicators, trend, grid_context)
        if grid is None:
            reasons.append("dynamic grid inputs unavailable or grid width exceeds 0.90%")
            breakdown["atr_grid_buy"] = 0.0
            breakdown["atr_grid_sell"] = 0.0
        else:
            grid_step, grid_buy, grid_sell, grid_buy_level, grid_sell_level = grid
            breakdown["atr_grid_buy"] = grid_buy
            breakdown["atr_grid_sell"] = grid_sell
            indicators["grid_anchor"] = indicators["vwap_0945"]
            indicators["grid_step"] = grid_step
            indicators["grid_buy_level"] = grid_buy_level
            indicators["grid_sell_level"] = grid_sell_level
    else:
        breakdown = None
    scores = _scores(breakdown)
    candidate = "WAIT"
    if enough_history and not invalid_data and indicators["vwap_0945"] is not None and breakdown:
        delta = breakdown["buy_total"] + breakdown.get("atr_grid_buy", 0.0) - breakdown["sell_total"] - breakdown.get("atr_grid_sell", 0.0)
        direction_confirmed = _confirmations(valid, indicators, breakdown)
        window = _window_allows(as_of or valid[-1].timestamp)
        buy_total = breakdown["buy_total"] + breakdown.get("atr_grid_buy", 0.0)
        sell_total = breakdown["sell_total"] + breakdown.get("atr_grid_sell", 0.0)
        if grid_step is not None and direction_confirmed[0] and window[0] and trend not in {"BEAR", "STRONG_BEAR"} and buy_total >= 72 and delta >= 12:
            candidate = "BUY"
        elif grid_step is not None and direction_confirmed[1] and window[1] and sell_total >= 68 and delta <= -12:
            candidate = "SELL"
        if not window[0] and not window[1]:
            reasons.append("outside V1.0 technical signal time windows")
        if trend == "STRONG_BEAR":
            reasons.append("STRONG_BEAR bans BUY and BUYBACK")
        elif trend == "BEAR":
            reasons.append("BEAR bans ordinary BUY")
    if candidate == "WAIT" and not reasons:
        reasons.append("technical threshold, confirmation, trend, or grid conditions not met")
    risk_status = "BLOCKED" if (
        not enough_history or invalid_data or indicators["vwap_0945"] is None
        or grid_step is None
    ) else "READY"
    return IntradayTechnicalAnalysis(
        symbol=symbol,
        trend=trend,
        indicators=indicators,
        scores=scores,
        candidate=candidate,
        risk_status=risk_status,
        reasons=tuple(dict.fromkeys(reasons)),
        score_breakdown=None if breakdown is None else {
            name: round(breakdown.get(name, 0.0), 2) for name in (
                "ema_buy", "boll_buy", "kdj_buy", "vwap_buy", "atr_grid_buy",
                "ema_sell", "boll_sell", "kdj_sell", "vwap_sell", "atr_grid_sell",
            )
        },
        grid_step=grid_step,
        parameter_hash=parameter_hash,
    )


def _indicators(bars: list[MinuteBar]) -> dict[str, Decimal | None]:
    closes = [bar.close for bar in bars]
    ema20 = _ema(closes, 20)
    ema60 = _ema(closes, 60)
    boll_mid = boll_upper = boll_lower = boll_sigma = None
    if len(closes) >= 20:
        window = closes[-20:]
        boll_mid = sum(window, ZERO) / Decimal(20)
        boll_sigma = (sum(((value - boll_mid) ** 2 for value in window), ZERO) / Decimal(20)).sqrt()
        boll_upper = boll_mid + Decimal(2) * boll_sigma
        boll_lower = boll_mid - Decimal(2) * boll_sigma
    kdj_k, kdj_d, kdj_j, kdj_prev_k, kdj_prev_d, kdj_min = _kdj(bars)
    atr = _wilder_atr(bars, 14)
    vwap, vwap_anchor = _session_vwaps(bars)
    return {
        "close": closes[-1] if closes else None,
        "ema_20": ema20, "ema_60": ema60,
        "bollinger_mid": boll_mid, "bollinger_upper": boll_upper, "bollinger_lower": boll_lower,
        "bollinger_sigma": boll_sigma,
        "kdj_k": kdj_k, "kdj_d": kdj_d, "kdj_j": kdj_j,
        "kdj_previous_k": kdj_prev_k, "kdj_previous_d": kdj_prev_d, "kdj_recent_min_k": kdj_min,
        "atr_14": atr, "vwap": vwap, "vwap_0945": vwap_anchor,
        "grid_anchor": vwap_anchor, "grid_step": None, "grid_buy_level": None, "grid_sell_level": None,
    }


def _ema(values: list[Decimal], period: int) -> Decimal | None:
    if len(values) < period:
        return None
    average = sum(values[:period], ZERO) / Decimal(period)
    alpha = Decimal(2) / Decimal(period + 1)
    for value in values[period:]:
        average += alpha * (value - average)
    return average


def _kdj(bars: list[MinuteBar]):
    if len(bars) < 9:
        return (None,) * 6
    k = d = Decimal(50)
    values = []
    for end in range(8, len(bars)):
        window = bars[end - 8 : end + 1]
        low = min(bar.low for bar in window)
        high = max(bar.high for bar in window)
        rsv = Decimal(50) if high == low else (bars[end].close - low) * Decimal(100) / (high - low)
        k = (Decimal(2) * k + rsv) / Decimal(3)
        d = (Decimal(2) * d + k) / Decimal(3)
        values.append((k, d))
    previous = values[-2] if len(values) > 1 else (None, None)
    return values[-1][0], values[-1][1], Decimal(3) * values[-1][0] - Decimal(2) * values[-1][1], previous[0], previous[1], min(value[0] for value in values[-2:])


def _wilder_atr(bars: list[MinuteBar], period: int) -> Decimal | None:
    if len(bars) <= period:
        return None
    true_ranges = [
        max(bar.high - bar.low, abs(bar.high - bars[index - 1].close), abs(bar.low - bars[index - 1].close))
        for index, bar in enumerate(bars) if index > 0
    ]
    atr = sum(true_ranges[:period], ZERO) / Decimal(period)
    for value in true_ranges[period:]:
        atr = (atr * Decimal(period - 1) + value) / Decimal(period)
    return atr


def _session_vwaps(bars: list[MinuteBar]) -> tuple[Decimal | None, Decimal | None]:
    if not bars:
        return None, None
    today = bars[-1].timestamp.astimezone(SHANGHAI).date()
    session = [bar for bar in bars if bar.timestamp.astimezone(SHANGHAI).date() == today]
    amount = volume = ZERO
    anchor = None
    for bar in session:
        stamp = bar.timestamp.astimezone(SHANGHAI).time()
        if stamp < time(9, 30):
            continue
        if bar.amount is None:
            return None, None
        amount += bar.amount
        volume += Decimal(bar.volume)
        if stamp == time(9, 45):
            anchor = None if volume == ZERO else amount / volume
    current = None if volume == ZERO else amount / volume
    return current, anchor


def _trend(bars: list[MinuteBar], indicators: dict[str, Decimal | None]) -> str | None:
    ema20, ema60, atr = indicators["ema_20"], indicators["ema_60"], indicators["atr_14"]
    previous = _ema([bar.close for bar in bars[:-3]], 20) if len(bars) >= 64 else None
    if ema20 is None or ema60 is None or atr is None or atr <= ZERO or previous is None:
        return None
    d, m = (ema20 - ema60) / atr, (ema20 - previous) / atr
    if d >= Decimal("0.90") and m >= Decimal("0.25"):
        return "STRONG_BULL"
    if d <= Decimal("-0.90") and m <= Decimal("-0.25"):
        return "STRONG_BEAR"
    if d >= Decimal("0.20") and m >= Decimal("0.05"):
        return "BULL"
    if d <= Decimal("-0.20") and m <= Decimal("-0.05"):
        return "BEAR"
    return "RANGE"


def _clip(value: Decimal) -> Decimal:
    return min(ONE, max(ZERO, value))


def _score_components(
    bars: list[MinuteBar], indicators: dict[str, Decimal | None], trend: str | None
) -> dict[str, float]:
    close = indicators["close"]
    mid, sigma = indicators["bollinger_mid"], indicators["bollinger_sigma"]
    atr, vwap = indicators["atr_14"], indicators["vwap"]
    if any(value is None for value in (close, mid, sigma, atr, vwap)) or atr <= ZERO:
        return {"ema_buy": 0.0, "ema_sell": 0.0, "boll_buy": 0.0, "boll_sell": 0.0,
                "kdj_buy": 0.0, "kdj_sell": 0.0, "vwap_buy": 0.0, "vwap_sell": 0.0,
                "atr_grid": 0.0, "buy_total": 0.0, "sell_total": 0.0}
    ema_buy, ema_sell = (20, 0) if trend in ("BULL", "STRONG_BULL") else (0, 20) if trend in ("BEAR", "STRONG_BEAR") else (10, 10)
    z = ZERO if sigma == ZERO else (close - mid) / (Decimal(2) * sigma)
    boll_buy = Decimal(20) * _clip((-z - Decimal("0.35")) / Decimal("0.65"))
    boll_sell = Decimal(20) * _clip((z - Decimal("0.35")) / Decimal("0.65"))
    k, d = indicators["kdj_k"], indicators["kdj_d"]
    pk, pd, recent_min = indicators["kdj_previous_k"], indicators["kdj_previous_d"], indicators["kdj_recent_min_k"]
    kdj_buy = 0
    kdj_sell = 0
    if all(value is not None for value in (k, d, pk, pd, recent_min)):
        if pk <= pd and k > d and recent_min <= 35:
            kdj_buy = 15
        elif k > d and k > pk and k <= 45:
            kdj_buy = 8
        if pk >= pd and k < d and max(pk, k) >= 65:
            kdj_sell = 15
        elif k < d and k < pk and k >= 55:
            kdj_sell = 8
    v = (close - vwap) / atr
    vwap_buy = Decimal(25) * _clip((-v - Decimal("0.30")) / Decimal("0.90"))
    vwap_sell = Decimal(25) * _clip((v - Decimal("0.30")) / Decimal("0.90"))
    return {
        "ema_buy": float(ema_buy), "ema_sell": float(ema_sell),
        "boll_buy": float(boll_buy), "boll_sell": float(boll_sell),
        "kdj_buy": float(kdj_buy), "kdj_sell": float(kdj_sell),
        "vwap_buy": float(vwap_buy), "vwap_sell": float(vwap_sell),
        "atr_grid": 0.0,
        "buy_total": float(ema_buy + boll_buy + kdj_buy + vwap_buy),
        "sell_total": float(ema_sell + boll_sell + kdj_sell + vwap_sell),
    }


def _grid(bars, indicators, trend, context):
    anchor, atr, close = indicators["vwap_0945"], indicators["atr_14"], indicators["close"]
    if context is None or anchor is None or atr is None or close is None:
        return None
    try:
        tick = Decimal(str(context["tick_size"]))
        cost = Decimal(str(context["round_trip_cost_rate"]))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None
    if tick <= ZERO or cost < ZERO or close <= ZERO:
        return None
    factor = Decimal("1.20") if trend in ("STRONG_BULL", "STRONG_BEAR") else ONE
    atr_ratio = atr / close
    if not Decimal("0.0008") <= atr_ratio <= Decimal("0.015"):
        return None
    ratio = max(Decimal("0.9") * factor * atr_ratio, Decimal("0.0025"), 3 * cost, 4 * tick / close)
    if ratio > Decimal("0.009"):
        return None
    calculated_step = (close * ratio / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    step = calculated_step
    session_date = bars[-1].timestamp.astimezone(SHANGHAI).date()
    if context.get("grid_date") == session_date:
        try:
            previous_step = Decimal(str(context["current_step"]))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            previous_step = None
        if previous_step is not None and previous_step > ZERO:
            stamp = bars[-1].timestamp.astimezone(SHANGHAI).time()
            reevaluate = stamp in {
                time(10, 0), time(10, 30), time(11, 0), time(13, 30), time(14, 0)
            }
            changed = abs(calculated_step - previous_step) / previous_step >= Decimal("0.20")
            if not reevaluate or not changed or context.get("pending_grid_orders", False):
                step = previous_step
    used = context.get("used_levels", ())
    if not isinstance(used, (tuple, list, set, frozenset)):
        return None
    latest_two = bars[-2:]
    down = up = None
    for level_index in range(3, 0, -1):
        level = anchor - step * level_index
        if any(bar.low <= level for bar in latest_two) and f"L{level_index}" not in used:
            down = (level_index, level)
            break
    for level_index in range(3, 0, -1):
        level = anchor + step * level_index
        if any(bar.high >= level for bar in latest_two) and f"U{level_index}" not in used:
            up = (level_index, level)
            break
    current = bars[-1]
    buy_score = buy_level = sell_score = sell_level = 0
    if down:
        idx, level = down
        buy_score = 20 if current.close > level else 10 if level - Decimal("0.1") * step <= current.close <= level else 0
        buy_level = f"L{idx}"
    if up:
        idx, level = up
        sell_score = 20 if current.close < level else 10 if level <= current.close <= level + Decimal("0.1") * step else 0
        sell_level = f"U{idx}"
    return step, buy_score, sell_score, buy_level, sell_level


def _confirmations(bars, indicators, scores):
    if len(bars) < 2:
        return False, False
    current, previous = bars[-1], bars[-2]
    k, d = indicators["kdj_k"], indicators["kdj_d"]
    pk, pd = indicators["kdj_previous_k"], indicators["kdj_previous_d"]
    golden = pk is not None and pd is not None and pk <= pd and k > d
    death = pk is not None and pd is not None and pk >= pd and k < d
    lower = indicators["bollinger_lower"]
    upper = indicators["bollinger_upper"]
    boll_buy = lower is not None and previous.low <= lower and current.close > lower
    boll_sell = upper is not None and previous.high >= upper and current.close < upper
    buy_level, sell_level = indicators["grid_buy_level"], indicators["grid_sell_level"]
    anchor, step = indicators["grid_anchor"], indicators["grid_step"]
    grid_buy = grid_sell = False
    if anchor is not None and step is not None and buy_level:
        index = int(buy_level[1:])
        level = anchor - step * index
        grid_buy = min(bar.low for bar in bars[-2:]) <= level and current.close > level
    if anchor is not None and step is not None and sell_level:
        index = int(sell_level[1:])
        level = anchor + step * index
        grid_sell = max(bar.high for bar in bars[-2:]) >= level and current.close < level
    rising = current.close > previous.close
    falling = current.close < previous.close
    return rising and (golden or boll_buy or grid_buy), falling and (death or boll_sell or grid_sell)


def _window_allows(as_of: datetime):
    stamp = as_of.astimezone(SHANGHAI).time()
    normal = time(9, 50) <= stamp < time(11, 20) or time(13, 10) <= stamp < time(14, 30)
    sell = normal or time(14, 30) <= stamp < time(14, 45)
    return normal, sell


def _scores(breakdown):
    if breakdown is None:
        return {"buy": None, "sell": None, "delta": None}
    buy = breakdown["buy_total"] + breakdown.get("atr_grid_buy", 0.0)
    sell = breakdown["sell_total"] + breakdown.get("atr_grid_sell", 0.0)
    return {"buy": round(buy, 2), "sell": round(sell, 2), "delta": round(buy - sell, 2)}
