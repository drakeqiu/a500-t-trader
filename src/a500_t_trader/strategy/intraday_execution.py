"""Per-symbol, fail-closed execution sizing for V1 intraday candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from typing import Mapping

from .intraday_technical import IntradayTechnicalAnalysis
from .intraday_v1_config import IntradayV1Config

ZERO = Decimal(0)
ONE = Decimal(1)
MAX_DAILY_TURNOVER_NAV = Decimal("1.50")
PORTFOLIO_LIMIT = Decimal("0.75")
RESERVED_CASH = Decimal("0.35")
MAX_INTENT_NAV = Decimal("0.08")
MAX_RISK_NAV = Decimal("0.0015")
MAX_SELL_TURNOVER_NAV = Decimal("0.10")
MAX_SYMBOL_T_VALUE = Decimal("0.25")
MAX_SYMBOL_HOLDING = Decimal("0.65")
MAX_PARTICIPATION = Decimal("0.02")
BUY_SCORE_THRESHOLD = 72
SELL_SCORE_THRESHOLD = 68


@dataclass(frozen=True, slots=True)
class SymbolExecution:
    symbol: str
    allowed: bool
    quantity: int
    side: str | None
    score: int | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IntradayExecution:
    nav: Decimal | None
    symbols: dict[str, SymbolExecution]
    reasons: tuple[str, ...]


def calculate_intraday_execution(
    *,
    analyses: Mapping[str, IntradayTechnicalAnalysis],
    config: IntradayV1Config,
    as_of: datetime,
    liquidity: Mapping[str, Mapping[str, object]],
    certification_passed: bool = False,
    daily_activity: Mapping[str, object] | None = None,
) -> IntradayExecution:
    """Size candidates against explicit account/rule inputs; never infer rules.

    Liquidity values must contain average_daily_amount, latest_bar_amount,
    latest_bar_volume, spread, and atr. Explicit freshness and daily-counter
    values are required before authorization; unknowns fail closed.
    """
    portfolio_reasons: list[str] = []
    nav = _nav(config, portfolio_reasons)
    results: dict[str, SymbolExecution] = {}
    ordered = sorted(
        analyses.items(),
        key=lambda item: (-(item[1].scores.get("buy") or -1), item[0]),
    )
    buy_allocated = ZERO
    turnover_allocated = ZERO
    sell_allocated = ZERO
    additions_allocated = ZERO
    fills_allocated = ZERO
    for symbol, analysis in ordered:
        reasons: list[str] = []
        try:
            candidate = analysis.candidate.upper()
            side = (
                "sell" if candidate in {"SELL", "TAKE_PROFIT"}
                else "buy" if candidate in {"BUY", "BUYBACK"}
                else candidate.lower()
            )
            if not certification_passed:
                reasons.append("V1 strategy certification has not passed")
            score = analysis.scores.get("buy" if side == "buy" else "sell")
            if analysis.risk_status != "READY":
                reasons.append("technical/risk candidate is not cleared for execution")
            if analysis.symbol != symbol:
                reasons.append("analysis symbol does not match requested symbol")
            if side not in {"buy", "sell"}:
                reasons.append("candidate is not actionable")
            if score is None:
                reasons.append("candidate score is unavailable")
            score_parameters = config.parameters.get("scores")
            signal_parameters = config.parameters.get("signals", {})
            if not isinstance(score_parameters, Mapping) or not isinstance(signal_parameters, Mapping):
                reasons.append("configured signal thresholds are unavailable")
                threshold = float("inf")
            elif candidate == "TAKE_PROFIT":
                threshold = score_parameters.get("take_profit", float("inf"))
            elif candidate == "BUYBACK":
                threshold = score_parameters.get("buyback", float("inf"))
                if analysis.trend == "BEAR":
                    threshold = score_parameters.get("weak_bear_buyback", float("inf"))
            else:
                threshold = signal_parameters.get(
                    "buy_threshold" if side == "buy" else "sell_threshold",
                    score_parameters.get("buy" if side == "buy" else "sell", float("inf")),
                )
            if not isinstance(threshold, (int, float)):
                threshold = float("inf")
                reasons.append("configured signal threshold is invalid")
            if score is not None and score < threshold:
                reasons.append(f"{candidate} score is below its configured threshold")
            reasons.extend(analysis.reasons)
            position_value = config.account["positions"].get(symbol, {})
            position = position_value if isinstance(position_value, dict) else {}
            security_value = config.securities.get(symbol)
            security = security_value if isinstance(security_value, dict) else {}
            if not security:
                reasons.append("security trading rules are missing")
            if nav is None:
                reasons.extend(portfolio_reasons)
            price = _decimal(analysis.indicators.get("close"))
            if price is None or price <= ZERO:
                reasons.append("current valid price is missing")
            fields_value = liquidity.get(symbol)
            fields = fields_value if isinstance(fields_value, Mapping) else {}
            if not fields:
                reasons.append("required liquidity data is missing")
            _validate_freshness_inputs(symbol, config, security, as_of, fields, reasons)
            _liquidity_reasons(fields, price, security, reasons)
            fee_rate = _decimal(config.fees.get("commission_rate"))
            minimum_fee = _decimal(config.fees.get("minimum_commission"))
            impact_bps = _decimal(config.fees.get("impact_bps"))
            if (
                fee_rate is None
                or fee_rate < ZERO
                or minimum_fee is None
                or minimum_fee < ZERO
                or impact_bps is None
                or impact_bps < ZERO
            ):
                reasons.append("required fee inputs are missing or invalid")
            risk = config.parameters.get("risk", {})
            activity = _activity_values(daily_activity)
            if activity is None:
                reasons.append("explicit current daily activity counters are missing")
            if _required_unquantified_limits_missing(config):
                reasons.append("required V1 risk limits are not explicitly configured")
            if activity is not None and _daily_limit_reasons(config, activity, side):
                reasons.extend(_daily_limit_reasons(config, activity, side))
            if activity is not None and side == "sell":
                sell_budget = _decimal(risk.get("daily_sell_limit"))
                if sell_budget is not None and (
                    activity["sell_notional"] + sell_allocated >= sell_budget
                ):
                    reasons.append("daily sell limit is reserved by higher-priority actions")
            if activity is not None and side == "buy":
                additions_limit = _decimal(risk.get("max_daily_additions"))
                if additions_limit is not None and (
                    activity["additions"] + additions_allocated >= additions_limit
                ):
                    reasons.append("daily addition limit is reserved by higher-priority actions")
            if activity is not None:
                fills_limit = _decimal(risk.get("max_daily_fills"))
                if fills_limit is not None and (
                    activity["fills"] + fills_allocated >= fills_limit
                ):
                    reasons.append("daily fill limit is reserved by higher-priority actions")
            if nav is None or price is None or not security or reasons:
                results[symbol] = SymbolExecution(
                    symbol, False, 0, side if side in {"buy", "sell"} else None,
                    score, tuple(dict.fromkeys(reasons)),
                )
                continue
            lot = _positive_int(security.get("lot_size"))
            tick = _decimal(security.get("tick_size"))
            if lot is None or tick is None or tick <= ZERO:
                reasons.append("valid trading unit or tick size is missing")
                results[symbol] = SymbolExecution(symbol, False, 0, side, score, tuple(reasons))
                continue
            t_plus = security.get("t_plus")
            if t_plus not in {"T+0", "T+1"}:
                reasons.append("valid T+0/T+1 settlement rule is missing")
            if (price / tick) != (price / tick).to_integral_value():
                reasons.append("current price is not aligned to the security tick size")
            lower_limit = _decimal(security.get("lower_price_limit"))
            upper_limit = _decimal(security.get("upper_price_limit"))
            if (
                lower_limit is None
                or upper_limit is None
                or lower_limit <= ZERO
                or upper_limit < lower_limit
            ):
                reasons.append("valid explicit security price bands are missing")
            elif price < lower_limit or price > upper_limit:
                reasons.append(
                    "current price is outside the explicit security price bands"
                )
            if (
                lower_limit is not None
                and upper_limit is not None
                and tick > ZERO
                and (
                    lower_limit / tick
                    != (lower_limit / tick).to_integral_value()
                    or upper_limit / tick
                    != (upper_limit / tick).to_integral_value()
                )
            ):
                reasons.append("explicit security price bands are not tick-aligned")
            holding = _nonnegative_int(position.get("quantity"))
            floor = _nonnegative_int(position.get("protected_floor"))
            sellable = _nonnegative_int(position.get("sellable_quantity"))
            market_value = _decimal(position.get("market_value"))
            if holding is None or floor is None or sellable is None or market_value is None:
                reasons.append("complete valid position quantities and valuation are required")
                results[symbol] = SymbolExecution(symbol, False, 0, side, score, tuple(reasons))
                continue
            if reasons:
                results[symbol] = SymbolExecution(symbol, False, 0, side, score, tuple(dict.fromkeys(reasons)))
                continue
            amount_per_share = price * (
                ONE
                + (impact_bps or ZERO) / Decimal(10000)
                + (fee_rate or ZERO)
            )
            if floor > holding or sellable > holding:
                reasons.append("protected floor or available inventory exceeds holdings")
                results[symbol] = SymbolExecution(
                    symbol, False, 0, side, score, tuple(reasons)
                )
                continue
            daily_turnover_limit = min(
                nav * MAX_DAILY_TURNOVER_NAV,
                _decimal(risk.get("daily_turnover_limit"))
                if isinstance(risk, dict)
                else ZERO,
            )
            daily_turnover_remaining = max(
                ZERO,
                daily_turnover_limit
                - activity["turnover_notional"]
                - turnover_allocated,
            )
            if side == "buy":
                headroom = max(ZERO, nav * PORTFOLIO_LIMIT - _held_value(config))
                symbol_headroom = max(
                    ZERO,
                    min(nav * MAX_SYMBOL_T_VALUE, nav * MAX_SYMBOL_HOLDING - market_value),
                )
                capacity = min(
                    nav * MAX_INTENT_NAV,
                    headroom,
                    symbol_headroom,
                    daily_turnover_remaining,
                    max(
                        ZERO,
                        _cash(config)
                        - nav * RESERVED_CASH
                        - buy_allocated
                        - (minimum_fee or ZERO),
                    ),
                )
                quantity = int(
                    (capacity / amount_per_share).to_integral_value(rounding=ROUND_FLOOR)
                )
            else:
                inventory = min(sellable, max(0, holding - floor))
                capacity = min(
                    nav * MAX_INTENT_NAV,
                    nav * MAX_SELL_TURNOVER_NAV,
                    market_value,
                    max(
                        ZERO,
                        _daily_sell_remaining(config, activity) - sell_allocated,
                    ),
                )
                quantity = min(
                    inventory,
                    int((capacity / price).to_integral_value(rounding=ROUND_FLOOR)),
                )
            quantity = quantity // lot * lot
            bar_volume = _decimal(fields.get("latest_bar_volume"))
            if bar_volume is None or bar_volume < ZERO:
                reasons.append("latest completed bar volume is invalid")
                quantity = 0
            else:
                participation_limit = int(
                    (bar_volume * MAX_PARTICIPATION).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                )
                quantity = min(quantity, participation_limit // lot * lot)
            if side == "buy":
                remaining = max(
                    ZERO, nav * PORTFOLIO_LIMIT - _held_value(config) - buy_allocated
                )
                quantity = min(
                    quantity,
                    int((remaining / amount_per_share).to_integral_value(rounding=ROUND_FLOOR))
                    // lot
                    * lot,
                )
            else:
                daily_remaining = _daily_sell_remaining(config, activity)
                quantity = min(
                    quantity,
                    int((daily_remaining / price).to_integral_value(rounding=ROUND_FLOOR))
                    // lot
                    * lot,
                )
            atr = _decimal(fields.get("atr"))
            grid_step = _decimal(fields.get("grid_step"))
            if atr is None or atr <= ZERO or grid_step is None or grid_step <= ZERO:
                reasons.append("positive ATR and V1 grid step are required for stop-risk sizing")
                results[symbol] = SymbolExecution(
                    symbol, False, 0, side, score, tuple(reasons)
                )
                continue
            stop_distance = max(
                Decimal("1.5") * atr,
                Decimal("1.2") * grid_step,
                Decimal("0.004") * price,
            )
            risk_budget = nav * MAX_RISK_NAV
            risk_quantity_by_stop = max(
                ZERO,
                (risk_budget - (minimum_fee or ZERO)) / stop_distance,
            ).to_integral_value(rounding=ROUND_FLOOR)
            risk_quantity_by_cost = (
                risk_budget
                / (stop_distance + price * (fee_rate or ZERO))
            ).to_integral_value(rounding=ROUND_FLOOR)
            quantity = min(
                quantity,
                int(min(risk_quantity_by_stop, risk_quantity_by_cost)) // lot * lot,
            )
            if quantity < lot:
                reasons.append(
                    "budget, portfolio, or available inventory is below one trading unit"
                )
                results[symbol] = SymbolExecution(
                    symbol, False, 0, side, score, tuple(reasons)
                )
                continue
            notional = Decimal(quantity) * price
            fee = max(minimum_fee or ZERO, notional * (fee_rate or ZERO))
            planned_risk = Decimal(quantity) * stop_distance
            if planned_risk + fee > nav * MAX_RISK_NAV:
                reasons.append("planned risk and fees exceed 0.15% of NAV")
                results[symbol] = SymbolExecution(
                    symbol, False, 0, side, score, tuple(reasons)
                )
                continue
            if side == "buy":
                buy_allocated += (
                    Decimal(quantity) * amount_per_share
                    + max(ZERO, (minimum_fee or ZERO) - notional * (fee_rate or ZERO))
                )
            turnover_allocated += notional
            if side == "sell":
                sell_allocated += notional
            if side == "buy":
                additions_allocated += ONE
            fills_allocated += ONE
        except Exception as error:
            results[symbol] = SymbolExecution(
                symbol, False, 0, None, None,
                (f"symbol evaluation failed: {type(error).__name__}",),
            )
    return IntradayExecution(nav, results, tuple(dict.fromkeys(portfolio_reasons)))


def _nav(config: IntradayV1Config, reasons: list[str]) -> Decimal | None:
    cash = _decimal(config.account.get("cash"))
    if cash is None or cash < ZERO:
        reasons.append("valid explicit cash balance is missing")
        return None
    value = cash
    positions = config.account.get("positions")
    if not isinstance(positions, dict):
        reasons.append("account positions are missing")
        return None
    for symbol, position in positions.items():
        if not isinstance(position, dict):
            reasons.append(f"account.positions.{symbol} is invalid")
            return None
        market_value = _decimal(position.get("market_value"))
        quantity = _nonnegative_int(position.get("quantity"))
        if market_value is None or market_value < ZERO or quantity is None:
            reasons.append(f"account.positions.{symbol}.market_value or quantity is invalid")
            return None
        value += market_value
    return value


def _held_value(config: IntradayV1Config) -> Decimal:
    total = ZERO
    positions = config.account.get("positions", {})
    if isinstance(positions, dict):
        for position in positions.values():
            if isinstance(position, dict):
                total += _decimal(position.get("market_value")) or ZERO
    return total


def _cash(config: IntradayV1Config) -> Decimal:
    return _decimal(config.account.get("cash")) or ZERO


def _validate_freshness_inputs(
    symbol: str, config: IntradayV1Config, security: Mapping[str, object],
    as_of: datetime, liquidity: Mapping[str, object], reasons: list[str],
) -> None:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        reasons.append("evaluation timestamp must include a timezone")
        return
    timing = config.parameters.get("timing", {})
    max_age = (
        _positive_int(timing.get("max_input_age_seconds"))
        if isinstance(timing, dict)
        else None
    )
    if max_age is None:
        reasons.append("explicit input freshness limits are missing")
        return
    sources = (
        ("account", config.account),
        ("fees", config.fees),
        (f"securities.{symbol}", security),
        ("market data", liquidity),
    )
    for label, source in sources:
        try:
            stamp = datetime.fromisoformat(str(source["as_of"]))
            age = (as_of - stamp).total_seconds()
            if stamp.tzinfo is None or stamp.utcoffset() is None or age < 0:
                reasons.append(f"{label} timestamp is invalid or later than evaluation time")
            elif age > max_age:
                reasons.append(f"{label} input has expired")
        except (KeyError, ValueError, TypeError):
            reasons.append(f"{label} timestamp is missing or invalid")


def _required_unquantified_limits_missing(config: IntradayV1Config) -> bool:
    risk = config.parameters.get("risk", {})
    timing = config.parameters.get("timing", {})
    required = ("daily_turnover_limit", "daily_sell_limit", "max_daily_additions", "max_daily_fills")
    return (
        not isinstance(risk, dict)
        or any(_decimal(risk.get(key)) is None for key in required)
        or not isinstance(timing, dict)
        or _positive_int(timing.get("max_input_age_seconds")) is None
    )


def _activity_values(activity: Mapping[str, object] | None) -> dict[str, Decimal] | None:
    if not isinstance(activity, Mapping):
        return None
    turnover = _decimal(activity.get("turnover_notional"))
    sell = _decimal(activity.get("sell_notional"))
    additions = _decimal(activity.get("additions"))
    fills = _decimal(activity.get("fills"))
    values = (turnover, sell, additions, fills)
    if any(value is None or value < ZERO for value in values):
        return None
    return {
        "turnover_notional": turnover,
        "sell_notional": sell,
        "additions": additions,
        "fills": fills,
    }


def _daily_limit_reasons(
    config: IntradayV1Config, activity: Mapping[str, Decimal], side: str
) -> tuple[str, ...]:
    risk = config.parameters.get("risk", {})
    if not isinstance(risk, dict):
        return ("explicit daily risk limits are missing",)
    max_turnover = _decimal(risk.get("daily_turnover_limit"))
    max_sell = _decimal(risk.get("daily_sell_limit"))
    max_additions = _decimal(risk.get("max_daily_additions"))
    max_fills = _decimal(risk.get("max_daily_fills"))
    reasons: list[str] = []
    if any(value is None or value < ZERO for value in (max_turnover, max_sell, max_additions, max_fills)):
        return ("explicit daily risk limits are missing or invalid",)
    if activity["turnover_notional"] >= max_turnover:
        reasons.append("daily turnover limit has been reached")
    if side == "sell" and activity["sell_notional"] >= max_sell:
        reasons.append("daily sell limit has been reached")
    if side == "buy" and activity["additions"] >= max_additions:
        reasons.append("daily addition limit has been reached")
    if activity["fills"] >= max_fills:
        reasons.append("daily fill limit has been reached")
    return tuple(reasons)


def _daily_sell_remaining(
    config: IntradayV1Config, activity: Mapping[str, Decimal]
) -> Decimal:
    risk = config.parameters.get("risk", {})
    limit = _decimal(risk.get("daily_sell_limit")) if isinstance(risk, dict) else None
    return max(ZERO, (limit or ZERO) - activity["sell_notional"])


def _liquidity_reasons(
    fields: Mapping[str, object], price: Decimal | None,
    security: Mapping[str, object], reasons: list[str],
) -> None:
    required = (
        "average_daily_amount",
        "latest_bar_amount",
        "latest_bar_volume",
        "spread",
        "atr",
    )
    values = {key: _decimal(fields.get(key)) for key in required}
    if any(value is None for value in values.values()) or price is None:
        reasons.append("required current liquidity/volatility fields are missing")
        return
    if any(value < ZERO for value in values.values()) or values["atr"] == ZERO:
        reasons.append("liquidity and volatility inputs must be nonnegative with positive ATR")
        return
    tick = _decimal(security.get("tick_size"))
    if tick is None:
        return
    if values["average_daily_amount"] < Decimal(30000000):
        reasons.append("20-day average daily amount is below 30000000")
    if values["latest_bar_amount"] < Decimal(1000000):
        reasons.append("latest completed 5-minute amount is below 1000000")
    if values["spread"] > max(Decimal("0.0015"), Decimal(2) * tick / price):
        reasons.append("spread exceeds V1 liquidity limit")
    atr_ratio = values["atr"] / price
    if not Decimal("0.0008") <= atr_ratio <= Decimal("0.015"):
        reasons.append("ATR/price is outside V1 liquidity limits")


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() else None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _positive_int(value: object) -> int | None:
    value = _nonnegative_int(value)
    return value if value is not None and value > 0 else None
