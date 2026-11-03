"""V1.0 technical analysis for completed five-minute ETF bars."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from ..market_data import SHANGHAI, MinuteBar
from .intraday_v1_config import RULE_SOURCE_SHA256, STRATEGY_ID, IntradayV1Config

ZERO = Decimal(0)
ONE = Decimal(1)
BAR_INTERVAL = 5
MINIMUM_HISTORY = 180
TECHNICAL_PARAMETER_VERSION = STRATEGY_ID

TECHNICAL_PARAMETERS = {
    "indicators": {
        "ema": [20, 60],
        "bollinger": [20, 2],
        "kdj": [9, 3, 3],
        "rsi": 14,
        "atr": 14,
    },
    "trend": {
        "bull_d": "0.20",
        "bull_m": "0.05",
        "strong_d": "0.90",
        "strong_m": "0.25",
    },
    "scores": {
        "weights": {
            "ema": 20,
            "bollinger": 20,
            "kdj": 8,
            "rsi": 7,
            "vwap": 25,
            "atr_grid": 20,
        },
        "bollinger_z_start": "0.35",
        "bollinger_z_full": "1.00",
        "vwap_atr_start": "0.30",
        "vwap_atr_full": "1.20",
        "direction_delta": 12,
        "buy": 72,
        "sell": 68,
        "take_profit": 60,
        "buyback": 68,
        "kdj_cross_buy_min": 35,
        "kdj_cross_sell_min": 65,
    },
    "grid": {
        "atr_multiplier": "0.9",
        "strong_trend_multiplier": "1.20",
        "minimum_ratio": "0.0025",
        "maximum_ratio": "0.009",
        "minimum_ticks": 4,
        "levels": 3,
        "anchor": "09:45",
        "reclaim_score_proximity": "0.1",
    },
    "timing": {
        "buy_sell_windows": [["09:50", "11:20"], ["13:10", "14:30"]],
        "no_buy_after": "14:30",
        "closeout_only_after": "14:45",
        "protection_only_after": "14:50",
    },
}
TECHNICAL_PARAMETER_HASH = hashlib.sha256(
    json.dumps(TECHNICAL_PARAMETERS, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
).hexdigest()


@dataclass(frozen=True, slots=True)
class IntradayTechnicalAnalysis:
    """Inspectable technical result; rule-match scores are not probabilities."""

    symbol: str
    trend: str | None
    indicators: Mapping[str, object]
    scores: dict[str, float | None]
    candidate: str
    risk_status: str
    reasons: tuple[str, ...]
    completed_valid_bars: int
    score_breakdown: dict[str, float] | None = None
    grid_step: Decimal | None = None
    confirmations: tuple[bool, bool] = (False, False)
    signal_windows: tuple[bool, bool] = (False, False)
    parameter_version: str = TECHNICAL_PARAMETER_VERSION
    parameter_hash: str = TECHNICAL_PARAMETER_HASH


def analyze_intraday_technical(
    bars_by_symbol: Mapping[str, tuple[MinuteBar, ...]],
    *,
    config: IntradayV1Config | None = None,
    grid_context: Mapping[str, Mapping[str, object]] | None = None,
    as_of: datetime | None = None,
    paired_state: Mapping[str, Mapping[str, object]] | None = None,
    score_weights: Mapping[str, float] | None = None,
) -> dict[str, IntradayTechnicalAnalysis]:
    """Score each symbol independently using tracked V1 rules."""
    if config is not None and (
        config.strategy_id != STRATEGY_ID
        or config.rule_version != "V1.0"
        or config.rule_source_sha256.lower() != RULE_SOURCE_SHA256
    ):
        raise ValueError(
            "technical analysis requires the tracked intraday ETF T V1.0 configuration"
        )
    parameters = TECHNICAL_PARAMETERS if config is None else config.parameters
    parameter_hash = TECHNICAL_PARAMETER_HASH if config is None else config.fingerprint
    weights = score_weights
    if weights is None:
        score_section = parameters.get("scores")
        weights = (
            score_section.get("weights")
            if isinstance(score_section, Mapping)
            else None
        )
    if not isinstance(weights, Mapping):
        raise ValueError("configured score weights are unavailable")
    if score_weights is not None:
        canonical_weights = json.dumps(
            dict(sorted(score_weights.items())), sort_keys=True, separators=(",", ":")
        )
        parameter_hash = hashlib.sha256(
            f"{parameter_hash}:{canonical_weights}".encode()
        ).hexdigest()
    return {
        symbol: _analyze_symbol(
            symbol,
            bars,
            (grid_context or {}).get(symbol),
            as_of,
            parameter_hash,
            parameters,
            weights,
            (paired_state or {}).get(symbol),
        )
        for symbol, bars in bars_by_symbol.items()
    }


def _analyze_symbol(
    symbol: str,
    supplied: tuple[MinuteBar, ...],
    grid_context: Mapping[str, object] | None,
    as_of: datetime | None,
    parameter_hash: str,
    parameters: Mapping[str, object],
    score_weights: Mapping[str, float],
    paired_context: Mapping[str, object] | None,
) -> IntradayTechnicalAnalysis:
    direction_confirmed = (False, False)
    window = (False, False)
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
    if _has_session_gap(valid):
        invalid_data = True
        reasons.append("missing completed five-minute bars within a trading session")
    if invalid_data:
        reasons.append("invalid or mismatched five-minute bar data")
    if len(valid) < MINIMUM_HISTORY:
        reasons.append("fewer than 180 completed valid 5-minute bars")
    indicators = _indicators(valid)
    output_indicators: dict[str, object] = dict(indicators)
    enough_history = len(valid) >= MINIMUM_HISTORY
    trend = _trend(valid, indicators) if enough_history else None
    if indicators["vwap_0945"] is None:
        reasons.append("missing 09:45 VWAP anchor")
    breakdown = (
        _score_components(valid, indicators, trend, score_weights)
        if enough_history
        else None
    )
    grid_step: Decimal | None = None
    grid_buy_level: str | None = None
    grid_sell_level: str | None = None
    if enough_history and breakdown is not None:
        grid = _grid(valid, indicators, trend, grid_context)
        if grid is None:
            reasons.append(
                "dynamic grid inputs unavailable or grid width exceeds 0.90%"
            )
            breakdown["atr_grid_buy"] = 0.0
            breakdown["atr_grid_sell"] = 0.0
        else:
            grid_step, grid_buy, grid_sell, grid_buy_level, grid_sell_level = grid
            breakdown["atr_grid_buy"] = grid_buy
            breakdown["atr_grid_sell"] = grid_sell
            indicators["grid_anchor"] = indicators["vwap_0945"]
            indicators["grid_step"] = grid_step
            output_indicators["grid_buy_level"] = grid_buy_level
            output_indicators["grid_sell_level"] = grid_sell_level
    else:
        breakdown = None
    scores = _scores(breakdown)
    candidate = "WAIT"
    if (
        enough_history
        and not invalid_data
        and indicators["vwap_0945"] is not None
        and breakdown
    ):
        delta = (
            breakdown["buy_total"]
            + breakdown.get("atr_grid_buy", 0.0)
            - breakdown["sell_total"]
            - breakdown.get("atr_grid_sell", 0.0)
        )
        direction_confirmed = _confirmations(
            valid, indicators, breakdown, grid_buy_level, grid_sell_level
        )
        window = _window_allows(as_of or valid[-1].timestamp)
        buy_total = breakdown["buy_total"] + breakdown.get("atr_grid_buy", 0.0)
        sell_total = breakdown["sell_total"] + breakdown.get("atr_grid_sell", 0.0)
        score_parameters = parameters.get("scores")
        signal_parameters = parameters.get("signals", {})
        if not isinstance(score_parameters, Mapping) or not isinstance(
            signal_parameters, Mapping
        ):
            reasons.append("configured signal thresholds are unavailable")
        else:
            buy_threshold = signal_parameters.get(
                "buy_threshold", score_parameters.get("buy")
            )
            sell_threshold = signal_parameters.get(
                "sell_threshold", score_parameters.get("sell")
            )
            delta_threshold = score_parameters.get("direction_delta")
            if not all(
                isinstance(value, (int, float))
                for value in (buy_threshold, sell_threshold, delta_threshold)
            ):
                reasons.append("configured signal thresholds are invalid")
            elif (
                grid_step is not None
                and direction_confirmed[0]
                and window[0]
                and trend not in {"BEAR", "STRONG_BEAR"}
                and isinstance(buy_threshold, (int, float))
                and isinstance(delta_threshold, (int, float))
                and buy_total >= buy_threshold
                and delta >= delta_threshold
            ):
                candidate = "BUY"
            elif (
                grid_step is not None
                and direction_confirmed[1]
                and window[1]
                and isinstance(sell_threshold, (int, float))
                and isinstance(delta_threshold, (int, float))
                and sell_total >= sell_threshold
                and delta <= -delta_threshold
            ):
                candidate = "SELL"

            if isinstance(paired_context, Mapping):
                take_profit = paired_context.get("take_profit")
                buyback = paired_context.get("buyback")
                tp_score = score_parameters.get("take_profit")
                bb_score = score_parameters.get("buyback")
                tp_price = (
                    _decimal_parameter(take_profit.get("target_price"))
                    if isinstance(take_profit, Mapping)
                    else None
                )
                bb_price = (
                    _decimal_parameter(buyback.get("target_price"))
                    if isinstance(buyback, Mapping)
                    else None
                )
                tp_quantity = (
                    take_profit.get("quantity")
                    if isinstance(take_profit, Mapping)
                    else None
                )
                bb_quantity = (
                    buyback.get("quantity") if isinstance(buyback, Mapping) else None
                )
                if (
                    tp_price is not None
                    and type(tp_quantity) is int
                    and tp_quantity > 0
                    and isinstance(tp_score, (int, float))
                    and isinstance(delta_threshold, (int, float))
                    and sell_total >= tp_score
                    and delta <= -delta_threshold
                    and direction_confirmed[1]
                    and window[1]
                    and valid[-1].close >= tp_price
                ):
                    candidate = "TAKE_PROFIT"
                elif (
                    bb_price is not None
                    and type(bb_quantity) is int
                    and bb_quantity > 0
                    and isinstance(bb_score, (int, float))
                    and isinstance(delta_threshold, (int, float))
                    and buy_total >= bb_score
                    and delta >= delta_threshold
                    and direction_confirmed[0]
                    and window[0]
                    and valid[-1].close <= bb_price
                    and trend != "STRONG_BEAR"
                ):
                    if trend == "BEAR":
                        weak_score = score_parameters.get("weak_bear_buyback")
                        if (
                            isinstance(weak_score, (int, float))
                            and buy_total >= weak_score
                            and _golden_cross(indicators)
                            and len(valid) >= 3
                            and valid[-1].close > valid[-2].close > valid[-3].close
                        ):
                            candidate = "BUYBACK"
                    else:
                        candidate = "BUYBACK"
        if not window[0] and not window[1]:
            reasons.append("outside V1.0 technical signal time windows")
        if trend == "STRONG_BEAR":
            reasons.append("STRONG_BEAR bans BUY and BUYBACK")
        elif trend == "BEAR":
            reasons.append("BEAR bans ordinary BUY")
    if candidate == "WAIT" and not reasons:
        reasons.append(
            "technical threshold, confirmation, trend, or grid conditions not met"
        )
    risk_status = (
        "BLOCKED"
        if (
            not enough_history
            or invalid_data
            or indicators["vwap_0945"] is None
            or grid_step is None
        )
        else "READY"
    )
    return IntradayTechnicalAnalysis(
        symbol=symbol,
        trend=trend,
        indicators=output_indicators if grid_step is not None else indicators,
        scores=scores,
        candidate=candidate,
        risk_status=risk_status,
        reasons=tuple(dict.fromkeys(reasons)),
        score_breakdown=None
        if breakdown is None
        else {
            name: round(breakdown.get(name, 0.0), 2)
            for name in (
                "ema_buy",
                "boll_buy",
                "kdj_buy",
                "vwap_buy",
                "atr_grid_buy",
                "rsi_buy",
                "ema_sell",
                "boll_sell",
                "kdj_sell",
                "vwap_sell",
                "rsi_sell",
                "atr_grid_sell",
            )
        },
        grid_step=grid_step,
        confirmations=direction_confirmed,
        signal_windows=window,
        parameter_hash=parameter_hash,
        completed_valid_bars=len(valid),
    )


def _decimal_parameter(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation, TypeError, ValueError:
        return None
    return parsed if parsed.is_finite() and parsed > ZERO else None


def _golden_cross(indicators: Mapping[str, object]) -> bool:
    k, d = indicators.get("kdj_k"), indicators.get("kdj_d")
    previous_k = indicators.get("kdj_previous_k")
    previous_d = indicators.get("kdj_previous_d")
    return (
        isinstance(k, Decimal)
        and isinstance(d, Decimal)
        and isinstance(previous_k, Decimal)
        and isinstance(previous_d, Decimal)
        and previous_k <= previous_d
        and k > d
    )


def _has_session_gap(bars: list[MinuteBar]) -> bool:
    for previous, current in zip(bars, bars[1:], strict=False):
        before = previous.timestamp.astimezone(SHANGHAI)
        after = current.timestamp.astimezone(SHANGHAI)
        if before.date() != after.date():
            continue
        if before.time() <= time(11, 30) and after.time() >= time(13, 0):
            continue
        if after - before != timedelta(minutes=BAR_INTERVAL):
            return True
    return False


def _indicators(bars: list[MinuteBar]) -> dict[str, Decimal | None]:
    closes = [bar.close for bar in bars]
    ema20 = _ema(closes, 20)
    ema60 = _ema(closes, 60)
    boll_mid = boll_upper = boll_lower = boll_sigma = None
    if len(closes) >= 20:
        window = closes[-20:]
        boll_mid = sum(window, ZERO) / Decimal(20)
        boll_sigma = (
            sum(((value - boll_mid) ** 2 for value in window), ZERO) / Decimal(20)
        ).sqrt()
        boll_upper = boll_mid + Decimal(2) * boll_sigma
        boll_lower = boll_mid - Decimal(2) * boll_sigma
    kdj_k, kdj_d, kdj_j, kdj_prev_k, kdj_prev_d, kdj_min = _kdj(bars)
    rsi, previous_rsi = _wilder_rsi(closes, 14)
    atr = _wilder_atr(bars, 14)
    vwap, vwap_anchor = _session_vwaps(bars)
    return {
        "close": closes[-1] if closes else None,
        "ema_20": ema20,
        "ema_60": ema60,
        "bollinger_mid": boll_mid,
        "bollinger_upper": boll_upper,
        "bollinger_lower": boll_lower,
        "bollinger_sigma": boll_sigma,
        "kdj_k": kdj_k,
        "kdj_d": kdj_d,
        "kdj_j": kdj_j,
        "kdj_previous_k": kdj_prev_k,
        "kdj_previous_d": kdj_prev_d,
        "kdj_recent_min_k": kdj_min,
        "rsi_14": rsi,
        "rsi_14_previous": previous_rsi,
        "atr_14": atr,
        "vwap": vwap,
        "vwap_0945": vwap_anchor,
        "grid_anchor": vwap_anchor,
        "grid_step": None,
        "grid_buy_level": None,
        "grid_sell_level": None,
    }


def _ema(values: list[Decimal], period: int) -> Decimal | None:
    if len(values) < period:
        return None
    average = sum(values[:period], ZERO) / Decimal(period)
    alpha = Decimal(2) / Decimal(period + 1)
    for value in values[period:]:
        average += alpha * (value - average)
    return average


def _kdj(
    bars: list[MinuteBar],
) -> tuple[
    Decimal | None,
    Decimal | None,
    Decimal | None,
    Decimal | None,
    Decimal | None,
    Decimal | None,
]:
    if len(bars) < 9:
        return (None,) * 6
    k = d = Decimal(50)
    values = []
    for end in range(8, len(bars)):
        window = bars[end - 8 : end + 1]
        low = min(bar.low for bar in window)
        high = max(bar.high for bar in window)
        rsv = (
            Decimal(50)
            if high == low
            else (bars[end].close - low) * Decimal(100) / (high - low)
        )
        k = (Decimal(2) * k + rsv) / Decimal(3)
        d = (Decimal(2) * d + k) / Decimal(3)
        values.append((k, d))
    previous = values[-2] if len(values) > 1 else (None, None)
    return (
        values[-1][0],
        values[-1][1],
        Decimal(3) * values[-1][0] - Decimal(2) * values[-1][1],
        previous[0],
        previous[1],
        min(value[0] for value in values[-2:]),
    )


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
    for value in true_ranges[period:]:
        atr = (atr * Decimal(period - 1) + value) / Decimal(period)
    return atr


def _session_vwaps(bars: list[MinuteBar]) -> tuple[Decimal | None, Decimal | None]:
    if not bars:
        return None, None
    today = bars[-1].timestamp.astimezone(SHANGHAI).date()
    session = [
        bar for bar in bars if bar.timestamp.astimezone(SHANGHAI).date() == today
    ]
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

def _wilder_rsi(
    values: list[Decimal], period: int
) -> tuple[Decimal | None, Decimal | None]:
    if len(values) <= period:
        return None, None
    gains = losses = ZERO
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains += max(change, ZERO)
        losses += max(-change, ZERO)
    average_gain = gains / Decimal(period)
    average_loss = losses / Decimal(period)
    current = _rsi_value(average_gain, average_loss)
    previous = None
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        average_gain = (
            average_gain * Decimal(period - 1) + max(change, ZERO)
        ) / Decimal(period)
        average_loss = (
            average_loss * Decimal(period - 1) + max(-change, ZERO)
        ) / Decimal(period)
        previous = current
        current = _rsi_value(average_gain, average_loss)
    return current, previous


def _rsi_value(average_gain: Decimal, average_loss: Decimal) -> Decimal:
    if average_gain == ZERO and average_loss == ZERO:
        return Decimal(50)
    if average_loss == ZERO:
        return Decimal(100)
    if average_gain == ZERO:
        return ZERO
    return Decimal(100) * average_gain / (average_gain + average_loss)


def _score_components(
    bars: list[MinuteBar],
    indicators: dict[str, Decimal | None],
    trend: str | None,
    weights: object,
) -> dict[str, float]:
    if not isinstance(weights, Mapping):
        raise ValueError("configured score weights are unavailable")
    kdj_weight = Decimal(str(weights.get("kdj", 8)))
    rsi_weight = Decimal(str(weights.get("rsi", 7)))
    close = indicators["close"]
    mid, sigma = indicators["bollinger_mid"], indicators["bollinger_sigma"]
    atr, vwap = indicators["atr_14"], indicators["vwap"]
    if (
        close is None
        or mid is None
        or sigma is None
        or atr is None
        or vwap is None
        or atr <= ZERO
    ):
        return {
            "ema_buy": 0.0,
            "ema_sell": 0.0,
            "boll_buy": 0.0,
            "boll_sell": 0.0,
            "kdj_buy": 0.0,
            "kdj_sell": 0.0,
            "rsi_buy": 0.0,
            "rsi_sell": 0.0,
            "vwap_buy": 0.0,
            "vwap_sell": 0.0,
            "atr_grid": 0.0,
            "buy_total": 0.0,
            "sell_total": 0.0,
        }
    ema_buy, ema_sell = (
        (20, 0)
        if trend in ("BULL", "STRONG_BULL")
        else (0, 20)
        if trend in ("BEAR", "STRONG_BEAR")
        else (10, 10)
    )
    z = ZERO if sigma == ZERO else (close - mid) / (Decimal(2) * sigma)
    boll_buy = Decimal(20) * _clip((-z - Decimal("0.35")) / Decimal("0.65"))
    boll_sell = Decimal(20) * _clip((z - Decimal("0.35")) / Decimal("0.65"))
    k, d = indicators["kdj_k"], indicators["kdj_d"]
    pk, pd, recent_min = (
        indicators["kdj_previous_k"],
        indicators["kdj_previous_d"],
        indicators["kdj_recent_min_k"],
    )
    kdj_buy = ZERO
    kdj_sell = ZERO
    if (
        k is not None
        and d is not None
        and pk is not None
        and pd is not None
        and recent_min is not None
    ):
        if pk <= pd and k > d and recent_min <= 35:
            kdj_buy = kdj_weight
        elif k > d and k > pk and k <= 45:
            kdj_buy = kdj_weight / Decimal(2)
        if pk >= pd and k < d and max(pk, k) >= 65:
            kdj_sell = kdj_weight
        elif k < d and k < pk and k >= 55:
            kdj_sell = kdj_weight / Decimal(2)
    rsi = indicators["rsi_14"]
    previous_rsi = indicators["rsi_14_previous"]
    rsi_buy = ZERO
    rsi_sell = ZERO
    if rsi is not None and previous_rsi is not None:
        if previous_rsi <= 30 and rsi > 30:
            rsi_buy = rsi_weight
        elif 30 < rsi <= 45 and rsi > previous_rsi:
            rsi_buy = rsi_weight * Decimal(4) / Decimal(7)
        if previous_rsi >= 70 and rsi < 70:
            rsi_sell = rsi_weight
        elif 55 <= rsi < 70 and rsi < previous_rsi:
            rsi_sell = rsi_weight * Decimal(4) / Decimal(7)
    v = (close - vwap) / atr
    vwap_buy = Decimal(25) * _clip((-v - Decimal("0.30")) / Decimal("0.90"))
    vwap_sell = Decimal(25) * _clip((v - Decimal("0.30")) / Decimal("0.90"))
    return {
        "ema_buy": float(ema_buy),
        "ema_sell": float(ema_sell),
        "boll_buy": float(boll_buy),
        "boll_sell": float(boll_sell),
        "kdj_buy": float(kdj_buy),
        "kdj_sell": float(kdj_sell),
        "rsi_buy": float(rsi_buy),
        "rsi_sell": float(rsi_sell),
        "vwap_buy": float(vwap_buy),
        "vwap_sell": float(vwap_sell),
        "atr_grid": 0.0,
        "buy_total": float(ema_buy + boll_buy + kdj_buy + rsi_buy + vwap_buy),
        "sell_total": float(ema_sell + boll_sell + kdj_sell + rsi_sell + vwap_sell),
    }


def _grid(
    bars: list[MinuteBar],
    indicators: Mapping[str, Decimal | None],
    trend: str | None,
    context: Mapping[str, object] | None,
) -> tuple[Decimal, int, int, str | None, str | None] | None:
    anchor, atr, close = (
        indicators["vwap_0945"],
        indicators["atr_14"],
        indicators["close"],
    )
    if context is None or anchor is None or atr is None or close is None:
        return None
    try:
        tick = Decimal(str(context["tick_size"]))
        cost = Decimal(str(context["round_trip_cost_rate"]))
    except KeyError, TypeError, ValueError, InvalidOperation:
        return None
    if tick <= ZERO or cost < ZERO or close <= ZERO:
        return None
    factor = Decimal("1.20") if trend in ("STRONG_BULL", "STRONG_BEAR") else ONE
    atr_ratio = atr / close
    if not Decimal("0.0008") <= atr_ratio <= Decimal("0.015"):
        return None
    ratio = max(
        Decimal("0.9") * factor * atr_ratio,
        Decimal("0.0025"),
        3 * cost,
        4 * tick / close,
    )
    if ratio > Decimal("0.009"):
        return None
    calculated_step = (close * ratio / tick).to_integral_value(
        rounding=ROUND_CEILING
    ) * tick
    step = calculated_step
    session_date = bars[-1].timestamp.astimezone(SHANGHAI).date()
    if context.get("grid_date") == session_date:
        try:
            previous_step = Decimal(str(context["current_step"]))
        except KeyError, TypeError, ValueError, InvalidOperation:
            previous_step = None
        if previous_step is not None and previous_step > ZERO:
            stamp = bars[-1].timestamp.astimezone(SHANGHAI).time()
            reevaluate = stamp in {
                time(10, 0),
                time(10, 30),
                time(11, 0),
                time(13, 30),
                time(14, 0),
            }
            changed = abs(calculated_step - previous_step) / previous_step >= Decimal(
                "0.20"
            )
            if (
                not reevaluate
                or not changed
                or context.get("pending_grid_orders", False)
            ):
                step = previous_step
    used = context.get("used_levels", ())
    if not isinstance(used, (tuple, list, set, frozenset)):
        return None
    latest_two = bars[-2:]
    down = up = None
    for level_index in range(3, 0, -1):
        level = anchor - step * level_index
        if (
            any(bar.low <= level for bar in latest_two)
            and f"L{level_index}" not in used
        ):
            down = (level_index, level)
            break
    for level_index in range(3, 0, -1):
        level = anchor + step * level_index
        if (
            any(bar.high >= level for bar in latest_two)
            and f"U{level_index}" not in used
        ):
            up = (level_index, level)
            break
    current = bars[-1]
    buy_level: str | None = None
    sell_level: str | None = None
    buy_score = 0
    sell_score = 0
    if down:
        idx, level = down
        buy_score = (
            20
            if current.close > level
            else 10
            if level - Decimal("0.1") * step <= current.close <= level
            else 0
        )
        buy_level = f"L{idx}"
    if up:
        idx, level = up
        sell_score = (
            20
            if current.close < level
            else 10
            if level <= current.close <= level + Decimal("0.1") * step
            else 0
        )
        sell_level = f"U{idx}"
    return step, buy_score, sell_score, buy_level, sell_level


def _confirmations(
    bars: list[MinuteBar],
    indicators: Mapping[str, Decimal | None],
    scores: Mapping[str, float],
    buy_level: str | None,
    sell_level: str | None,
) -> tuple[bool, bool]:
    if len(bars) < 2:
        return False, False
    current, previous = bars[-1], bars[-2]
    k, d = indicators["kdj_k"], indicators["kdj_d"]
    pk, pd = indicators["kdj_previous_k"], indicators["kdj_previous_d"]
    golden = (
        pk is not None
        and pd is not None
        and k is not None
        and d is not None
        and pk <= pd
        and k > d
    )
    death = (
        pk is not None
        and pd is not None
        and k is not None
        and d is not None
        and pk >= pd
        and k < d
    )
    lower = indicators["bollinger_lower"]
    upper = indicators["bollinger_upper"]
    boll_buy = lower is not None and previous.low <= lower and current.close > lower
    boll_sell = upper is not None and previous.high >= upper and current.close < upper
    anchor, step = indicators["grid_anchor"], indicators["grid_step"]
    grid_buy = grid_sell = False
    if anchor is not None and step is not None and buy_level:
        index = int(buy_level[1:])
        level = anchor - step * index
        grid_buy = min(bar.low for bar in bars[-2:]) <= level and current.close > level
    if anchor is not None and step is not None and sell_level:
        index = int(sell_level[1:])
        level = anchor + step * index
        grid_sell = (
            max(bar.high for bar in bars[-2:]) >= level and current.close < level
        )
    rising = current.close > previous.close
    falling = current.close < previous.close
    return rising and (golden or boll_buy or grid_buy), falling and (
        death or boll_sell or grid_sell
    )


def _window_allows(as_of: datetime) -> tuple[bool, bool]:
    stamp = as_of.astimezone(SHANGHAI).time()
    normal = time(9, 50) <= stamp < time(11, 20) or time(13, 10) <= stamp < time(14, 30)
    sell = normal or time(14, 30) <= stamp < time(14, 45)
    return normal, sell


def _scores(
    breakdown: Mapping[str, float] | None,
) -> dict[str, float | None]:
    if breakdown is None:
        return {"buy": None, "sell": None, "delta": None}
    buy = breakdown["buy_total"] + breakdown.get("atr_grid_buy", 0.0)
    sell = breakdown["sell_total"] + breakdown.get("atr_grid_sell", 0.0)
    return {"buy": round(buy, 2), "sell": round(sell, 2), "delta": round(buy - sell, 2)}
