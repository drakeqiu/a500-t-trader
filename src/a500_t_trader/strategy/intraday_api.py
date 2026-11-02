"""Public advisory strategy API with separate candidate and execution veto."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Mapping
from zoneinfo import ZoneInfo

from ..market_data import MinuteBar
from .intraday_execution import calculate_intraday_execution
from .intraday_state import IntradayStrategyStateStore, StrategyRecommendation
from .intraday_technical import IntradayTechnicalAnalysis, analyze_intraday_technical
from .intraday_v1_config import IntradayV1Config
from .premarket import MAX_INPUT_AGE

SHANGHAI = ZoneInfo("Asia/Shanghai")


def evaluate_intraday_strategy(
    bars_by_symbol: Mapping[str, tuple[MinuteBar, ...]],
    *,
    as_of: datetime,
    config: IntradayV1Config | None,
    liquidity: Mapping[str, Mapping[str, object]] | None = None,
    daily_activity: Mapping[str, object] | None = None,
    certification: Mapping[str, object] | None = None,
    state_store: IntradayStrategyStateStore | None = None,
    pending_order_state: str | None = None,
) -> dict[str, dict[str, object]]:
    """Return a deterministic result per symbol; unknown gates always veto.

    Exact source scoring rules and an integrated certification verifier are not
    available on this integration tip. The real execution sizing gate is still
    run when the explicit V1 configuration is supplied, with certification
    fixed false until verifiable evidence can be bound to that configuration.

    Persistent state is mandatory for directional candidates. Recommendations
    are recorded only after every gate passes and never count as actual fills.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("strategy timestamp must include a timezone")
    now = as_of.astimezone(SHANGHAI)
    securities = {} if config is None else config.securities
    account = {} if config is None else config.account
    positions = account.get("positions")
    account_symbols = positions if isinstance(positions, dict) else {}
    symbols = sorted(set(bars_by_symbol) | set(securities) | set(account_symbols))
    analyses = analyze_intraday_technical(bars_by_symbol)
    adjusted = {
        symbol: _with_reference_price(analysis, _latest_completed_bar(bars_by_symbol.get(symbol, ())))
        for symbol, analysis in analyses.items()
    }
    sized = (
        {}
        if config is None
        else calculate_intraday_execution(
            analyses=adjusted,
            config=config,
            as_of=as_of,
            liquidity=liquidity or {},
            certification_passed=False,
            daily_activity=daily_activity,
        ).symbols
    )
    results: dict[str, dict[str, object]] = {}
    for symbol in symbols:
        analysis = analyses.get(symbol)
        reasons: list[str] = []
        if config is None:
            reasons.extend(("account snapshot and V1 configuration unavailable", f"trading rules unavailable for {symbol}"))
        else:
            account_stamp = _timestamp(account.get("as_of"))
            if account_stamp is None:
                reasons.append("account snapshot timestamp unavailable or invalid")
            elif _stale(account_stamp, now):
                reasons.append("account snapshot is stale or future-dated")
            position = account.get("positions", {}).get(symbol) if isinstance(account.get("positions"), dict) else None
            if not isinstance(position, dict):
                reasons.append(f"account position unavailable for {symbol}")
            else:
                for field in ("quantity", "protected_floor", "cost_basis", "sellable_quantity", "market_value"):
                    if position.get(field) is None:
                        reasons.append(f"account position {field} unavailable for {symbol}")
            rule = securities.get(symbol)
            if rule is None:
                reasons.append(f"trading rules unavailable for {symbol}")
            else:
                stamp = _timestamp(rule.get("as_of"))
                if stamp is None:
                    reasons.append(f"trading rules for {symbol} have invalid timestamp")
                elif _stale(stamp, now):
                    reasons.append(f"trading rules for {symbol} are stale or future-dated")

        side = analysis.candidate.lower() if analysis is not None else ""
        state_parameters = _state_parameters(config)
        state_reasons: tuple[str, ...] = ()
        if state_store is None or state_parameters is None:
            state_reasons = ("persistent strategy state unavailable or unconfigured",)
        elif side in ("buy", "sell"):
            try:
                (
                    grid_version,
                    grid_layer,
                    grid_round,
                    max_daily_trades,
                    max_daily_adds,
                    max_daily_turnover,
                    min_same_direction_bars,
                ) = state_parameters
                state_reasons = state_store.check_constraints(
                    symbol=symbol,
                    session_date=now.date(),
                    side=side,
                    now=now,
                    grid_version=grid_version,
                    grid_layer=grid_layer,
                    grid_round=grid_round,
                    pending_order_state=pending_order_state,
                    max_daily_trades=max_daily_trades,
                    max_daily_adds=max_daily_adds,
                    max_daily_turnover=max_daily_turnover,
                    min_same_direction_bars=min_same_direction_bars,
                )
            except (ValueError, TypeError, ArithmeticError, sqlite3.Error):
                state_reasons = ("persistent strategy state unavailable or unconfigured",)
        reasons.extend(state_reasons)
        bars = bars_by_symbol.get(symbol, ())
        latest = _latest_completed_bar(bars)
        if latest is None:
            reasons.append(f"market data unavailable for {symbol}")
        elif _stale(latest.timestamp.astimezone(SHANGHAI), now):
            reasons.append(f"market data for {symbol} is stale or future-dated")
        if analysis is None:
            reasons.append(f"technical candidate unavailable for {symbol}")
        else:
            reasons.extend(analysis.reasons)
        reasons.extend(_certification_reasons(certification, now))
        execution = sized.get(symbol)
        if execution is None:
            reasons.append("V1 quantity calculation unavailable without explicit configuration")
            quantity = None
            allowed = False
            execution_reasons: tuple[str, ...] = ()
        else:
            reasons.extend(execution.reasons)
            quantity = execution.quantity if execution.allowed else 0
            allowed = execution.allowed and not reasons
            execution_reasons = execution.reasons
        if analysis is not None and analysis.candidate == "WAIT":
            allowed = False
            quantity = 0 if execution is not None else None
        reasons = list(dict.fromkeys(reasons))
        if (
            analysis is not None
            and side in ("buy", "sell")
            and state_store is not None
            and state_parameters is not None
            and allowed
        ):
            grid_version, grid_layer, grid_round = state_parameters[:3]
            recommendation_id = (
                f"{config.strategy_id}:{symbol}:{now.isoformat()}:{side}:"
                f"{grid_version}:{grid_layer}:{grid_round}"
            )
            state_store.record_recommendation(
                StrategyRecommendation(
                    recommendation_id,
                    symbol,
                    side,
                    now,
                    grid_version,
                    grid_layer,
                    grid_round,
                )
            )
        results[symbol] = {
            "signal": _signal(analysis),
            "position": _position(account, symbol),
            "execution": {
                "allowed": allowed,
                "quantity": quantity,
                "suggested_quantity": quantity,
                "reference_price": None if latest is None else latest.close,
                "price": None,
                "suggested_price": None,
                "order_type": None,
                "expires_at": None,
                "reasons": list(execution_reasons),
            },
            "risk": {
                "status": "READY" if allowed else "BLOCKED",
                "vetoed": not allowed,
                "constraints": _constraints(account, symbol, securities.get(symbol)),
            },
            "reasons": reasons,
        }
    return results

def _state_parameters(
    config: IntradayV1Config | None,
) -> tuple[int, int, int, int, int, Decimal, int] | None:
    if config is None:
        return None
    grid = config.parameters.get("grid")
    risk = config.parameters.get("risk")
    if not isinstance(grid, dict) or not isinstance(risk, dict):
        return None
    values = (
        grid.get("grid_version"),
        grid.get("grid_layer"),
        grid.get("grid_round"),
        risk.get("max_daily_trades"),
        risk.get("max_daily_adds"),
        risk.get("max_daily_turnover"),
        risk.get("min_same_direction_bars"),
    )
    if any(type(value) is not int for value in values[:5] + values[6:]):
        return None
    try:
        turnover = Decimal(str(values[5]))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not turnover.is_finite() or turnover <= 0:
        return None
    grid_version, grid_layer, grid_round, trades, adds, _, bars = values
    if grid_version < 1 or grid_layer < 0 or grid_round < 0 or trades < 1 or adds < 1 or bars < 0:
        return None
    return grid_version, grid_layer, grid_round, trades, adds, turnover, bars


def _latest_completed_bar(bars: tuple[MinuteBar, ...]) -> MinuteBar | None:
    completed = (bar for bar in bars if bar.is_completed)
    return max(completed, key=lambda bar: bar.timestamp, default=None)


def _with_reference_price(
    analysis: IntradayTechnicalAnalysis, latest: MinuteBar | None
) -> IntradayTechnicalAnalysis:
    if latest is None:
        return analysis
    indicators = dict(analysis.indicators)
    indicators["close"] = latest.close
    return replace(analysis, indicators=indicators)


def _stale(timestamp: datetime, now: datetime) -> bool:
    age = now - timestamp
    return age < timedelta(0) or age > MAX_INPUT_AGE


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _certification_reasons(
    certification: Mapping[str, object] | None, now: datetime
) -> list[str]:
    if certification is None:
        return ["certification evidence unavailable; no verifiable certification gate is installed"]
    if certification.get("status") != "passed":
        return ["certification is failed or not passed"]
    timestamp = _timestamp(certification.get("as_of"))
    if timestamp is not None and _stale(timestamp.astimezone(SHANGHAI), now):
        return ["certification is stale or future-dated"]
    return ["certification evidence is unverified or not bound to current inputs"]


def _position(account: Mapping[str, object], symbol: str) -> dict[str, object]:
    positions = account.get("positions")
    value = positions.get(symbol) if isinstance(positions, dict) else None
    position = value if isinstance(value, dict) else {}
    return {
        "holding_quantity": position.get("quantity"),
        "sellable_quantity": position.get("sellable_quantity"),
        "cost_basis": position.get("cost_basis"),
        "available_cash": account.get("cash"),
        "constraints": ["position sizing is unavailable while any V1 execution gate is blocked"],
    }


def _constraints(
    account: Mapping[str, object],
    symbol: str,
    rule: Mapping[str, object] | None,
) -> dict[str, object]:
    positions = account.get("positions")
    value = positions.get(symbol) if isinstance(positions, dict) else None
    result: dict[str, object] = {
        "position": value if isinstance(value, dict) else None,
        "cash": account.get("cash"),
        "minimum_holding": None,
        "maximum_position": None,
    }
    if rule is not None:
        result["trading_rules"] = dict(rule)
    return result


def _signal(analysis: IntradayTechnicalAnalysis | None) -> dict[str, object]:
    if analysis is None:
        return {
            "direction": None,
            "candidate": None,
            "trend": None,
            "scores": {"buy": None, "sell": None},
            "indicators": {},
            "reasons": ["technical candidate unavailable"],
        }
    return {
        "direction": analysis.candidate.lower() if analysis.candidate != "WAIT" else "none",
        "candidate": analysis.candidate,
        "trend": analysis.trend,
        "scores": analysis.scores,
        "indicators": analysis.indicators,
        "reasons": list(analysis.reasons),
    }
