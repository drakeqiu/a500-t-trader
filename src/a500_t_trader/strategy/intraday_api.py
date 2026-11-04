"""Public advisory strategy API with separate candidate and execution veto."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import cast
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
    state_store: IntradayStrategyStateStore | None = None,
    pending_order_state: str | None = None,
    paired_state: Mapping[str, Mapping[str, object]] | None = None,
    grid_context: Mapping[str, Mapping[str, object]] | None = None,
    risk_exit_required: Mapping[str, bool] | None = None,
) -> dict[str, dict[str, object]]:
    """Evaluate source-exact candidates, then apply durable and sizing vetoes.

    The technical grid context is explicit because its cost rate is
    quantity-specific. Missing context blocks a candidate in the analyzer.
    ``paired_state`` is a per-symbol mapping with optional ``take_profit`` and
    ``buyback`` records; missing or malformed pair data never creates an intent.
    ``risk_exit_required`` carries an explicit upstream risk decision and
    bypasses only the RSI momentum gate, not execution constraints.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("strategy timestamp must include a timezone")
    now = as_of.astimezone(SHANGHAI)
    securities = {} if config is None else config.securities
    account = {} if config is None else config.account
    positions = account.get("positions")
    account_symbols = positions if isinstance(positions, dict) else {}
    symbols = sorted(set(bars_by_symbol) | set(securities) | set(account_symbols))
    analyses: dict[str, IntradayTechnicalAnalysis] = {}
    analyzer_error: str | None = None
    if config is not None:
        try:
            analyses = analyze_intraday_technical(
                bars_by_symbol,
                config=config,
                grid_context=grid_context,
                as_of=as_of,
                paired_state=paired_state,
            )
            analyses = {
                symbol: _route_forced_risk_exit(
                    analysis, (risk_exit_required or {}).get(symbol, False)
                )
                for symbol, analysis in analyses.items()
            }
            analyses = {
                symbol: _filter_rsi_momentum(analysis)
                for symbol, analysis in analyses.items()
            }
        except (ValueError, TypeError, ArithmeticError) as error:
            analyzer_error = f"source-exact technical analysis unavailable: {error}"
    else:
        analyzer_error = (
            "source-exact technical analysis unavailable without V1 configuration"
        )

    results: dict[str, dict[str, object]] = {}
    for symbol in symbols:
        analysis = analyses.get(symbol)
        reasons: list[str] = []
        if config is None:
            reasons.extend(
                (
                    "account snapshot and V1 configuration unavailable",
                    f"trading rules unavailable for {symbol}",
                )
            )
        else:
            account_stamp = _timestamp(account.get("as_of"))
            if account_stamp is None:
                reasons.append("account snapshot timestamp unavailable or invalid")
            elif _stale(account_stamp, now):
                reasons.append("account snapshot is stale or future-dated")
            position = positions.get(symbol) if isinstance(positions, dict) else None
            if not isinstance(position, dict):
                reasons.append(f"account position unavailable for {symbol}")
            else:
                for field in (
                    "quantity",
                    "protected_floor",
                    "cost_basis",
                    "sellable_quantity",
                    "market_value",
                ):
                    if position.get(field) is None:
                        reasons.append(
                            f"account position {field} unavailable for {symbol}"
                        )
            rule = securities.get(symbol)
            if rule is None:
                reasons.append(f"trading rules unavailable for {symbol}")
            else:
                stamp = _timestamp(rule.get("as_of"))
                if stamp is None:
                    reasons.append(f"trading rules for {symbol} have invalid timestamp")
                elif _stale(stamp, now):
                    reasons.append(
                        f"trading rules for {symbol} are stale or future-dated"
                    )
        if analyzer_error is not None:
            reasons.append(analyzer_error)
        elif analysis is None:
            reasons.append(f"technical candidate unavailable for {symbol}")
        else:
            reasons.extend(
                reason
                for reason in analysis.reasons
                if not (
                    analysis.candidate == "FORCED_RISK_EXIT"
                    and reason == "RSI_NOT_READY"
                )
            )

        side = (
            "sell"
            if analysis is not None
            and analysis.candidate in {"TAKE_PROFIT", "FORCED_RISK_EXIT"}
            else "buy"
            if analysis is not None and analysis.candidate == "BUYBACK"
            else analysis.candidate.lower()
            if analysis is not None
            else ""
        )
        pair_trade_pair_id: str | None = None
        pair_max_quantity = 0
        if analysis is not None and analysis.candidate in {"TAKE_PROFIT", "BUYBACK"}:
            pair_context = (paired_state or {}).get(symbol)
            pair_key = (
                "take_profit" if analysis.candidate == "TAKE_PROFIT" else "buyback"
            )
            pair_record = (
                pair_context.get(pair_key)
                if isinstance(pair_context, Mapping)
                else None
            )
            pair_id = (
                pair_record.get("trade_pair_id")
                if isinstance(pair_record, Mapping)
                else None
            )
            try:
                persisted_pair = (
                    state_store.trade_pair(pair_id)
                    if state_store is not None and isinstance(pair_id, str)
                    else None
                )
            except sqlite3.Error, TypeError, ValueError:
                persisted_pair = None
            valid_pair = (
                isinstance(pair_id, str)
                and bool(pair_id.strip())
                and persisted_pair is not None
                and persisted_pair.get("symbol") == symbol
                and persisted_pair.get("status") == "open"
            )
            if valid_pair and isinstance(persisted_pair, Mapping):
                buy_quantity = persisted_pair.get("buy_quantity")
                sell_quantity = persisted_pair.get("sell_quantity")
                buy_outstanding = (
                    buy_quantity - sell_quantity
                    if isinstance(buy_quantity, int) and isinstance(sell_quantity, int)
                    else 0
                )
                sell_outstanding = (
                    sell_quantity - buy_quantity
                    if isinstance(buy_quantity, int) and isinstance(sell_quantity, int)
                    else 0
                )
                if (
                    analysis.candidate == "TAKE_PROFIT"
                    and persisted_pair.get("buy_fill_id")
                    and buy_outstanding > 0
                ):
                    pair_max_quantity = buy_outstanding
                elif (
                    analysis.candidate == "BUYBACK"
                    and persisted_pair.get("sell_fill_id")
                    and sell_outstanding > 0
                ):
                    pair_max_quantity = sell_outstanding
                else:
                    valid_pair = False
            if valid_pair:
                pair_trade_pair_id = pair_id
            else:
                reasons.append(
                    "paired intent does not match an open persisted pair "
                    "with eligible fills"
                )
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
            except ValueError, TypeError, ArithmeticError, sqlite3.Error:
                state_reasons = (
                    "persistent strategy state unavailable or unconfigured",
                )
        reasons.extend(state_reasons)
        bars = bars_by_symbol.get(symbol, ())
        latest = _latest_completed_bar(bars)
        if latest is None:
            reasons.append(f"market data unavailable for {symbol}")
        elif _stale(latest.timestamp.astimezone(SHANGHAI), now):
            reasons.append(f"market data for {symbol} is stale or future-dated")

        # Never size a technical WAIT or a candidate blocked by an upstream gate.
        eligible = (
            analysis is not None
            and side in ("buy", "sell")
            and not reasons
            and config is not None
            and latest is not None
        )
        execution = None
        if (
            eligible
            and analysis is not None
            and latest is not None
            and config is not None
        ):
            adjusted = _with_reference_price(analysis, latest)
            try:
                execution = calculate_intraday_execution(
                    analyses={symbol: adjusted},
                    config=config,
                    as_of=as_of,
                    liquidity=liquidity or {},
                    daily_activity=daily_activity,
                ).symbols.get(symbol)
            except ValueError, TypeError, ArithmeticError:
                reasons.append("V1 quantity calculation unavailable")
        if execution is None:
            if side in ("buy", "sell") and eligible:
                reasons.append("V1 quantity calculation unavailable")
            quantity = 0
            allowed = False
            execution_reasons: tuple[str, ...] = ()
        else:
            reasons.extend(execution.reasons)
            quantity = execution.quantity if execution.allowed and not reasons else 0
            if (
                quantity
                and analysis is not None
                and analysis.candidate in {"TAKE_PROFIT", "BUYBACK"}
            ):
                context = (paired_state or {}).get(symbol, {})
                key = (
                    "take_profit" if analysis.candidate == "TAKE_PROFIT" else "buyback"
                )
                pair = context.get(key) if isinstance(context, Mapping) else None
                pair_quantity = (
                    pair.get("quantity") if isinstance(pair, Mapping) else None
                )
                security = securities.get(symbol)
                lot_value = (
                    security.get("lot_size") if isinstance(security, dict) else None
                )
                lot = lot_value if type(lot_value) is int and lot_value > 0 else None
                if type(pair_quantity) is not int or pair_quantity <= 0 or lot is None:
                    quantity = 0
                    reasons.append("paired quantity or lot size unavailable")
                else:
                    quantity = (
                        min(quantity, pair_quantity, pair_max_quantity) // lot * lot
                    )
                    if analysis.candidate == "BUYBACK" and analysis.trend == "BEAR":
                        quantity = min(quantity, (pair_max_quantity // 4) // lot * lot)
                    if quantity < lot:
                        quantity = 0
                        reasons.append(
                            "eligible paired quantity is below one trading unit"
                        )
            allowed = execution.allowed and quantity > 0 and not reasons
            execution_reasons = execution.reasons
        if analysis is not None and analysis.candidate == "WAIT":
            allowed = False
            quantity = 0
        reasons = list(dict.fromkeys(reasons))
        if (
            config is not None
            and analysis is not None
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
            try:
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
            except ValueError, TypeError, sqlite3.Error:
                reasons.append(
                    "persistent strategy recommendation could not be recorded"
                )
                allowed = False
                quantity = 0
            reasons = list(dict.fromkeys(reasons))
        signal = _signal(analysis)
        if pair_trade_pair_id is not None:
            signal["trade_pair_id"] = pair_trade_pair_id
        results[symbol] = {
            "signal": signal,
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


def _route_forced_risk_exit(
    analysis: IntradayTechnicalAnalysis,
    required: bool,
) -> IntradayTechnicalAnalysis:
    """Route an explicit upstream risk decision without applying RSI momentum."""
    if required is not True or analysis.risk_status != "READY":
        return analysis
    reasons = [reason for reason in analysis.reasons if not reason.startswith("RSI_")]
    if (
        _rsi_value(analysis.indicators.get("rsi_14")) is None
        or _rsi_value(analysis.indicators.get("rsi_14_previous")) is None
    ):
        reasons.append("RSI_NOT_READY")
    return replace(
        analysis,
        candidate="FORCED_RISK_EXIT",
        reasons=tuple(reasons),
    )


def _filter_rsi_momentum(
    analysis: IntradayTechnicalAnalysis,
) -> IntradayTechnicalAnalysis:
    """Apply the RSI hard gate after price/KDJ analysis has formed a candidate."""
    if analysis.candidate not in {"BUY", "BUYBACK", "SELL"}:
        return analysis

    current = _rsi_value(analysis.indicators.get("rsi_14"))
    previous = _rsi_value(analysis.indicators.get("rsi_14_previous"))
    if current is None or previous is None:
        reason = "RSI_NOT_READY"
    elif analysis.candidate in {"BUY", "BUYBACK"}:
        if current > 30 and current > previous:
            return analysis
        reason = "RSI_BUY_MOMENTUM_NOT_CONFIRMED"
    else:
        if current < 70 and current < previous:
            return analysis
        reason = "RSI_SELL_MOMENTUM_NOT_CONFIRMED"
    return replace(
        analysis,
        candidate="WAIT",
        reasons=tuple(dict.fromkeys((*analysis.reasons, reason))),
    )


def _rsi_value(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation, TypeError, ValueError:
        return None
    return parsed if parsed.is_finite() else None


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
    if not all(type(value) is int for value in values[:5] + values[6:]):
        return None
    grid_version, grid_layer, grid_round, trades, adds, _, bars = cast(
        tuple[int, int, int, int, int, object, int], values
    )
    try:
        turnover = Decimal(str(values[5]))
    except InvalidOperation, TypeError, ValueError:
        return None
    if not turnover.is_finite() or turnover <= 0:
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
    return (
        parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    )


def _position(account: Mapping[str, object], symbol: str) -> dict[str, object]:
    positions = account.get("positions")
    value = positions.get(symbol) if isinstance(positions, dict) else None
    position = value if isinstance(value, dict) else {}
    return {
        "holding_quantity": position.get("quantity"),
        "sellable_quantity": position.get("sellable_quantity"),
        "cost_basis": position.get("cost_basis"),
        "available_cash": account.get("cash"),
        "constraints": [
            "position sizing is unavailable while any V1 execution gate is blocked"
        ],
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
        "direction": analysis.candidate.lower()
        if analysis.candidate != "WAIT"
        else "none",
        "candidate": analysis.candidate,
        "trend": analysis.trend,
        "scores": analysis.scores,
        "indicators": analysis.indicators,
        "reasons": list(analysis.reasons),
    }
