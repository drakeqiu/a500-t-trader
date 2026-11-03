"""Signal-driven, auditable intraday bar execution simulation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from enum import StrEnum
from typing import Protocol

from ..market_data import MinuteBar
from ..strategy.intraday_technical import (
    IntradayTechnicalAnalysis,
    analyze_intraday_technical,
)
from ..strategy.intraday_v1_config import IntradayV1Config
from ..strategy.premarket import AccountSnapshot

BASIS_POINTS = Decimal(10000)
BOARD_LOT = 100


class StrategyRulesUnavailableError(ValueError):
    """Raised when no source-defined strategy signal API is available."""


class SignalIntent(StrEnum):
    ORDINARY = "ordinary"
    BUYBACK = "buyback"
    TAKE_PROFIT = "take_profit"
    RISK_EXIT = "risk_exit"


@dataclass(frozen=True, slots=True)
class BarSignal:
    timestamp: datetime
    direction: str
    quantity: int
    intent: SignalIntent | str = SignalIntent.ORDINARY
    pair_id: str | None = None
    atr: Decimal | None = None
    grid_step: Decimal | None = None
    grid_level: str | None = None

    def __post_init__(self) -> None:
        if self.direction not in ("buy", "sell"):
            raise ValueError("signal direction must be buy or sell")
        if self.quantity <= 0 or self.quantity % BOARD_LOT:
            raise ValueError("signal quantity must be a positive board lot")
        try:
            intent = SignalIntent(self.intent)
        except ValueError as error:
            raise ValueError("unsupported signal intent") from error
        object.__setattr__(self, "intent", intent)
        if intent is SignalIntent.BUYBACK and self.direction != "buy":
            raise ValueError("buyback intent must be a buy")
        if self.atr is not None and self.atr <= 0:
            raise ValueError("signal ATR must be positive")
        if self.grid_step is not None and self.grid_step <= 0:
            raise ValueError("signal grid step must be positive")

class StrategySignalProvider(Protocol):
    def signals(
        self, symbol: str, bars: tuple[MinuteBar, ...]
    ) -> tuple[BarSignal, ...]: ...

class EventDrivenSignalProvider(Protocol):
    def signals_at(
        self,
        symbol: str,
        bars: tuple[MinuteBar, ...],
        account_state: Mapping[str, object],
    ) -> tuple[BarSignal, ...]: ...



class CandidateQuantityResolver(Protocol):
    """Resolve order quantity from explicit downstream account/risk inputs."""

    def quantity_for(
        self,
        symbol: str,
        analysis: IntradayTechnicalAnalysis,
        bars: tuple[MinuteBar, ...],
    ) -> int | None: ...


class TechnicalGridContextProvider(Protocol):
    def context_for(
        self, symbol: str, bars: tuple[MinuteBar, ...]
    ) -> Mapping[str, object] | None: ...


class TechnicalAnalysisSignalProvider:
    """Adapt causal V1 technical candidates to simulator signals.

    A quantity resolver is mandatory because the technical analyzer does not
    authorize position sizing or account eligibility. Blocked/WAIT results
    never reach it. An optional history bound reduces replay cost when callers
    explicitly accept a finite warm-up window.
    """

    def __init__(
        self,
        *,
        quantity_for: CandidateQuantityResolver,
        config: IntradayV1Config | None = None,
        grid_context: Mapping[str, Mapping[str, object]] | None = None,
        grid_context_for: TechnicalGridContextProvider | None = None,
        max_history_bars: int | None = None,
        score_weights: Mapping[str, float] | None = None,
        rsi_momentum_filter: bool = False,
    ) -> None:
        if max_history_bars is not None and max_history_bars <= 0:
            raise ValueError("max_history_bars must be positive")
        self._quantity_for = quantity_for
        self._config = config
        self._grid_context = grid_context
        self._grid_context_for = grid_context_for
        self._max_history_bars = max_history_bars
        self._score_weights = score_weights
        self._rsi_momentum_filter = rsi_momentum_filter

    def signals(
        self, symbol: str, bars: tuple[MinuteBar, ...]
    ) -> tuple[BarSignal, ...]:
        signals: list[BarSignal] = []
        history: list[MinuteBar] = []
        for bar in bars:
            if not bar.is_completed:
                continue
            history.append(bar)
            if (
                self._max_history_bars is not None
                and len(history) > self._max_history_bars
            ):
                del history[: len(history) - self._max_history_bars]
            history_tuple = tuple(history)
            context = (
                self._grid_context_for.context_for(symbol, history_tuple)
                if self._grid_context_for is not None
                else (self._grid_context or {}).get(symbol)
            )
            analysis = analyze_intraday_technical(
                {symbol: history_tuple},
                config=self._config,
                grid_context=None if context is None else {symbol: context},
                as_of=bar.timestamp,
                score_weights=self._score_weights,
            )[symbol]
            intent = (
                "buyback"
                if analysis.candidate == "BUYBACK"
                else "take_profit"
                if analysis.candidate == "TAKE_PROFIT"
                else "ordinary"
            )
            direction = "buy" if analysis.candidate == "BUYBACK" else analysis.candidate.lower()
            if (
                self._rsi_momentum_filter
                and direction in {"buy", "sell"}
                and not _rsi_momentum_allowed(analysis, direction)
            ):
                continue
            if analysis.risk_status != "READY" or direction not in {"buy", "sell"}:
                continue
            quantity = self._quantity_for.quantity_for(symbol, analysis, history_tuple)
            if quantity is not None:
                signals.append(BarSignal(bar.timestamp, direction, quantity, intent))
        return tuple(signals)


def _rsi_momentum_allowed(analysis: IntradayTechnicalAnalysis, direction: str) -> bool:
    current = analysis.indicators.get("rsi_14")
    previous = analysis.indicators.get("rsi_14_previous")
    if not isinstance(current, Decimal) or not isinstance(previous, Decimal):
        return False
    if direction == "buy":
        return current > Decimal(30) and current > previous
    return direction == "sell" and current < Decimal(70) and current < previous


@dataclass(frozen=True, slots=True)
class IntradayBacktestParams:
    participation_rate: Decimal = Decimal("0.1")
    fee_bps: Decimal = Decimal(0)
    slippage_bps: Decimal = Decimal(0)
    order_lifetime_bars: int = 1
    minimum_commission: Decimal = Decimal(0)
    dynamic_sizing: bool = False
    protected_floor: int | None = None
    protected_floor_fraction: Decimal = Decimal("0.70")

    def __post_init__(self) -> None:
        if not Decimal(0) < self.participation_rate <= Decimal(1):
            raise ValueError("participation_rate must be in (0, 1]")
        if self.fee_bps < 0 or self.slippage_bps < 0 or self.minimum_commission < 0:
            raise ValueError("fees and slippage cannot be negative")
        if self.order_lifetime_bars < 1:
            raise ValueError("order_lifetime_bars must be positive")
        if self.protected_floor is not None and (
            self.protected_floor < 0 or self.protected_floor % BOARD_LOT
        ):
            raise ValueError("protected_floor must be a non-negative board lot")
        if not Decimal(0) <= self.protected_floor_fraction <= Decimal(1):
            raise ValueError("protected_floor_fraction must be in [0, 1]")



@dataclass(frozen=True, slots=True)
class IntradayFill:
    side: str
    quantity: int
    signal_timestamp: datetime
    fill_timestamp: datetime
    fill_price: Decimal
    fees: Decimal
    slippage: Decimal
    intent: SignalIntent = SignalIntent.ORDINARY
    pair_id: str | None = None
    atr: Decimal | None = None
    grid_step: Decimal | None = None
    grid_level: str | None = None

@dataclass(frozen=True, slots=True)
class IntradayBacktestResult:
    status: str
    symbol: str
    fills: tuple[IntradayFill, ...]
    cancellations: tuple[dict[str, object], ...]
    snapshot_id: str
    snapshot_json: str
    snapshot_bar_count: int
    strategy_parameters: dict[str, object]
    implementation_version: str
    assumptions: dict[str, object]
    initial_cash: Decimal
    initial_holdings: int
    final_cash: Decimal
    final_holdings: int
    strategy_final_value: Decimal
    baseline_final_value: Decimal
    strategy_return: Decimal
    baseline_return: Decimal
    account_states: tuple[dict[str, object], ...]
    pair_ledger: tuple[dict[str, object], ...]


@dataclass(slots=True)
class _Order:
    signal: BarSignal
    submitted_index: int
    remaining: int
    reserved_cash: Decimal


def run_intraday_backtest(
    *,
    symbol: str,
    account: AccountSnapshot,
    bars: tuple[MinuteBar, ...],
    signals: tuple[BarSignal, ...] | StrategySignalProvider | EventDrivenSignalProvider | None,
    params: IntradayBacktestParams,
    strategy_parameters: dict[str, object],
    implementation_version: str,
) -> IntradayBacktestResult:
    """Simulate source signals with prior-event state and next-bar fills."""
    if signals is None:
        raise StrategyRulesUnavailableError(
            "V1 strategy signal provider is unavailable; "
            "source-defined rules are required"
        )
    if not implementation_version.strip():
        raise ValueError("implementation_version must not be empty")
    selected = tuple(
        sorted((bar for bar in bars if bar.is_completed), key=lambda bar: bar.timestamp)
    )
    if len(selected) < 2:
        raise ValueError("at least two completed bars are required")
    if any(bar.symbol != symbol for bar in selected):
        raise ValueError("all bars must match the selected symbol")
    if len({bar.timestamp for bar in selected}) != len(selected):
        raise ValueError("bar timestamps must be unique")
    if any(bar.timestamp.tzinfo is None for bar in selected):
        raise ValueError("bar timestamps must include time zones")
    if account.as_of > selected[0].timestamp:
        raise ValueError("account snapshot must not follow first bar")
    event_provider = (
        signals if hasattr(signals, "signals_at") else None
    )
    if event_provider is not None:
        signals = ()
    elif not isinstance(signals, tuple):
        signals = signals.signals(symbol, selected)
    times = {bar.timestamp for bar in selected}
    if any(signal.timestamp not in times for signal in signals):
        raise ValueError("every signal must refer to a completed input bar")
    if tuple(sorted(signals, key=lambda signal: signal.timestamp)) != signals:
        raise ValueError("signals must be in timestamp order")

    signal_at: dict[datetime, list[BarSignal]] = {}
    for signal in signals:
        signal_at.setdefault(signal.timestamp, []).append(signal)
    submitted_signals = list(signals)
    initial_cash = account.available_cash
    initial_holdings = account.holdings.get(symbol, 0)
    protected_floor = (
        params.protected_floor
        if params.protected_floor is not None
        else int(
            (
                Decimal(initial_holdings)
                * params.protected_floor_fraction
                / Decimal(BOARD_LOT)
            ).to_integral_value(rounding=ROUND_DOWN)
        )
        * BOARD_LOT
    )
    cash = initial_cash
    holdings = initial_holdings
    sellable = min(initial_holdings, account.sellable.get(symbol, 0))
    acquired: list[tuple[date, int]] = []
    orders: list[_Order] = []
    fills: list[IntradayFill] = []
    cancellations: list[dict[str, object]] = []
    account_states: list[dict[str, object]] = []
    pair_ledger: list[dict[str, object]] = []
    pair_sequence = 1
    daily_sell_notional = Decimal(0)
    daily_turnover = Decimal(0)
    daily_fill_count = 0
    daily_addition_count = 0
    current_day: date | None = None
    for index, bar in enumerate(selected):
        day = bar.timestamp.date()
        if current_day != day:
            current_day = day
            daily_sell_notional = Decimal(0)
            daily_turnover = Decimal(0)
            daily_fill_count = 0
            daily_addition_count = 0
        newly_sellable = sum(
            quantity for acquired_day, quantity in acquired if acquired_day < day
        )
        sellable += newly_sellable
        acquired = [
            (acquired_day, quantity)
            for acquired_day, quantity in acquired
            if acquired_day >= day
        ]
        if index:
            max_fill = (
                int(
                    Decimal(bar.volume)
                    * (
                        min(params.participation_rate, Decimal("0.02"))
                        if params.dynamic_sizing
                        else params.participation_rate
                    )
                )
                // BOARD_LOT
                * BOARD_LOT
            )
            for order_position, order in enumerate(tuple(orders)):
                if index <= order.submitted_index:
                    continue
                quantity = min(order.remaining, max_fill)
                if order.signal.intent == "buyback":
                    pair = next(
                        (
                            item
                            for item in pair_ledger
                            if item["pair_id"] == order.signal.pair_id
                            and item["status"] == "open"
                        ),
                        None,
                    )
                    pending_ahead = sum(
                        candidate.remaining
                        for candidate in orders
                        if candidate is not order
                        and candidate.signal.intent == "buyback"
                        and candidate.signal.pair_id == order.signal.pair_id
                        and candidate.submitted_index < order.submitted_index
                    )
                    quantity = min(
                        quantity,
                        max(0, int(pair["remaining_quantity"]) - pending_ahead)
                        if pair is not None
                        else 0,
                    )
                if order.signal.direction == "buy" and quantity:
                    execution_price = bar.open * (
                        Decimal(1) + params.slippage_bps / BASIS_POINTS
                    )
                    unit_cost = execution_price * (
                        Decimal(1) + params.fee_bps / BASIS_POINTS
                    )
                    available = min(cash, order.reserved_cash)
                    quantity = min(
                        quantity,
                        int(
                            (available / (unit_cost * BOARD_LOT)).to_integral_value(
                                rounding=ROUND_DOWN
                            )
                        )
                        * BOARD_LOT,
                    )
                    if quantity and (
                        execution_price * quantity * params.fee_bps / BASIS_POINTS
                        < params.minimum_commission
                    ):
                        fee_budget = available - params.minimum_commission
                        quantity = (
                            0
                            if fee_budget < 0
                            else min(
                                quantity,
                                int(
                                    (
                                        fee_budget / (execution_price * BOARD_LOT)
                                    ).to_integral_value(rounding=ROUND_DOWN)
                                )
                                * BOARD_LOT,
                            )
                        )
                if params.dynamic_sizing and quantity:
                    if daily_fill_count >= 8:
                        quantity = 0
                    else:
                        nav_at_fill = cash + holdings * bar.open
                        fill_price = bar.open * (
                            Decimal(1)
                            + (
                                params.slippage_bps / BASIS_POINTS
                                if order.signal.direction == "buy"
                                else -params.slippage_bps / BASIS_POINTS
                            )
                        )
                        turnover_room = max(
                            Decimal(0), Decimal("1.50") * nav_at_fill - daily_turnover
                        )
                        quantity = min(
                            quantity,
                            int(
                                (turnover_room / fill_price).to_integral_value(
                                    rounding=ROUND_DOWN
                                )
                            )
                            // BOARD_LOT
                            * BOARD_LOT,
                        )
                        if order.signal.direction == "buy":
                            other_pending = sum(
                                candidate.remaining
                                for candidate in orders
                                if candidate is not order
                                and candidate.signal.direction == "buy"
                            )
                            position_room = max(
                                Decimal(0),
                                Decimal("0.65") * nav_at_fill
                                - holdings * fill_price
                                - other_pending * fill_price,
                            )
                            trading_room = max(
                                Decimal(0),
                                Decimal("0.25") * nav_at_fill
                                - max(0, holdings - min(protected_floor, holdings))
                                * fill_price
                                - other_pending * fill_price,
                            )
                            quantity = min(
                                quantity,
                                int(
                                    (position_room / fill_price).to_integral_value(
                                        rounding=ROUND_DOWN
                                    )
                                )
                                // BOARD_LOT
                                * BOARD_LOT,
                                int(
                                    (trading_room / fill_price).to_integral_value(
                                        rounding=ROUND_DOWN
                                    )
                                )
                                // BOARD_LOT
                                * BOARD_LOT,
                            )
                        if (
                            order.signal.direction == "sell"
                            and order.signal.intent is not SignalIntent.RISK_EXIT
                        ):
                            reserved_sell_notional = sum(
                                (
                                    candidate.remaining
                                    * bar.open
                                    * (
                                        Decimal(1)
                                        - params.slippage_bps / BASIS_POINTS
                                    )
                                    for candidate in tuple(orders)[:order_position]
                                    if candidate.signal.direction == "sell"
                                    and candidate.signal.intent
                                    is not SignalIntent.RISK_EXIT
                                ),
                                Decimal(0),
                            )
                            sell_room = max(
                                Decimal(0),
                                Decimal("0.10") * nav_at_fill
                                - daily_sell_notional
                                - reserved_sell_notional,
                            )
                            quantity = min(
                                quantity,
                                int(
                                    (sell_room / fill_price).to_integral_value(
                                        rounding=ROUND_DOWN
                                    )
                                )
                                // BOARD_LOT
                                * BOARD_LOT,
                            )
                if quantity:
                    if order.signal.direction == "buy":
                        fill_price = bar.open * (
                            Decimal(1) + params.slippage_bps / BASIS_POINTS
                        )
                    else:
                        fill_price = bar.open * (
                            Decimal(1) - params.slippage_bps / BASIS_POINTS
                        )
                    notional = fill_price * quantity
                    fees = max(
                        notional * params.fee_bps / BASIS_POINTS,
                        params.minimum_commission,
                    )
                    if order.signal.direction == "buy":
                        slippage = (fill_price - bar.open) * quantity
                        cash -= notional + fees
                        holdings += quantity
                        acquired.append((day, quantity))
                        order.reserved_cash = max(
                            Decimal(0), order.reserved_cash - notional - fees
                        )
                    else:
                        slippage = (bar.open - fill_price) * quantity
                        cash += notional - fees
                        holdings -= quantity
                    fills.append(
                        IntradayFill(
                            order.signal.direction,
                            quantity,
                            order.signal.timestamp,
                            bar.timestamp,
                            fill_price,
                            fees,
                            slippage,
                            order.signal.intent,
                            order.signal.pair_id,
                            order.signal.atr,
                            order.signal.grid_step,
                            order.signal.grid_level,
                        )
                    )
                    if order.signal.direction == "sell":
                        daily_sell_notional += notional
                        if order.signal.intent not in {"buyback", "risk_exit", "take_profit"}:
                            pair_ledger.append(
                                {
                                    "pair_id": f"pair-{pair_sequence}",
                                    "sell_timestamp": bar.timestamp.isoformat(),
                                    "sell_price": str(fill_price),
                                    "sell_quantity": quantity,
                                    "sell_gross": str(notional),
                                    "sell_grid_step": (
                                        str(order.signal.grid_step)
                                        if order.signal.grid_step is not None
                                        else None
                                    ),
                                    "sell_intent": str(order.signal.intent),
                                    "sell_fees": str(fees),
                                    "buyback_gross": "0",
                                    "buyback_fees": "0",
                                    "realized_net_pnl": None,
                                    "buyback_quantity": 0,
                                    "remaining_quantity": quantity,
                                    "status": "open",
                                    "intent": order.signal.intent,
                                }
                            )
                            pair_sequence += 1
                    elif order.signal.intent == "buyback":
                        pair = next(
                            (item for item in pair_ledger if item["pair_id"] == order.signal.pair_id),
                            None,
                        )
                        if pair is not None:
                            pair["remaining_quantity"] = int(pair["remaining_quantity"]) - quantity
                            pair["buyback_quantity"] = int(pair["buyback_quantity"]) + quantity
                            pair["buyback_gross"] = str(
                                Decimal(str(pair["buyback_gross"])) + notional
                            )
                            pair["buyback_fees"] = str(
                                Decimal(str(pair["buyback_fees"])) + fees
                            )
                            if pair["remaining_quantity"] == 0:
                                pair["status"] = "closed"
                                pair["buyback_timestamp"] = bar.timestamp.isoformat()
                                pair["realized_net_pnl"] = str(
                                    Decimal(str(pair["sell_gross"]))
                                    - Decimal(str(pair["buyback_gross"]))
                                    - Decimal(str(pair["sell_fees"]))
                                    - Decimal(str(pair["buyback_fees"]))
                                )
                    else:
                        daily_addition_count += 1
                    daily_turnover += notional
                    daily_fill_count += 1
                    order.remaining -= quantity
                    max_fill = max(0, max_fill - quantity)
                if order.remaining == 0:
                    orders.remove(order)
                elif index - order.submitted_index >= params.order_lifetime_bars:
                    if order.signal.direction == "sell":
                        sellable += order.remaining
                    cancellations.append(
                        {
                            "signal_timestamp": order.signal.timestamp,
                            "direction": order.signal.direction,
                            "unfilled_quantity": order.remaining,
                            "reserved_cash_released": order.reserved_cash,
                        }
                    )
                    orders.remove(order)
        bar_signals = signal_at.get(bar.timestamp, ())
        if event_provider is not None:
            decision_state: dict[str, object] = {
                "timestamp": bar.timestamp.isoformat(),
                "bar_index": index,
                "available_cash": str(cash),
                "holdings": holdings,
                "sellable": sellable,
                "nav": str(cash + holdings * bar.close),
                "reserved_cash": str(
                    sum(
                        (order.reserved_cash for order in orders),
                        Decimal(0),
                    )
                ),
                "pending_orders": tuple(
                    {
                        "direction": order.signal.direction,
                        "intent": order.signal.intent,
                        "quantity": order.remaining,
                        "pair_id": order.signal.pair_id,
                    }
                    for order in orders
                ),
                "fills": tuple(
                    {
                        "side": fill.side,
                        "quantity": fill.quantity,
                        "signal_timestamp": fill.signal_timestamp.isoformat(),
                        "fill_timestamp": fill.fill_timestamp.isoformat(),
                        "fill_price": str(fill.fill_price),
                        "fees": str(fill.fees),
                        "intent": fill.intent.value,
                        "pair_id": fill.pair_id,
                        "atr": str(fill.atr) if fill.atr is not None else None,
                        "grid_step": (
                            str(fill.grid_step)
                            if fill.grid_step is not None
                            else None
                        ),
                        "grid_level": fill.grid_level,
                    }
                    for fill in fills
                ),
                "daily_turnover": str(daily_turnover),
                "daily_fill_count": daily_fill_count,
                "daily_addition_count": daily_addition_count,
                "remaining_daily_sell_notional": str(
                    max(
                        Decimal(0),
                        Decimal("0.10") * (cash + holdings * bar.close)
                        - daily_sell_notional
                        - sum(
                            (
                                order.remaining * bar.close
                                for order in orders
                                if order.signal.direction == "sell"
                                and order.signal.intent is not SignalIntent.RISK_EXIT
                            ),
                            Decimal(0),
                        ),
                    )
                ),
                "remaining_daily_turnover": str(
                    max(
                        Decimal(0),
                        Decimal("1.50") * (cash + holdings * bar.close)
                        - daily_turnover
                        - sum(
                            (order.remaining * bar.close for order in orders),
                            Decimal(0),
                        ),
                    )
                ),
                "remaining_daily_fills": max(
                    0, 8 - daily_fill_count - len(orders)
                ),
                "remaining_daily_additions": max(
                    0,
                    3
                    - daily_addition_count
                    - sum(
                        1
                        for order in orders
                        if order.signal.direction == "buy"
                        and order.signal.intent is not SignalIntent.BUYBACK
                    ),
                ),
                "free_cash": str(
                    max(
                        Decimal(0),
                        cash
                        - sum(
                            (
                                order.reserved_cash
                                for order in orders
                                if order.signal.direction == "buy"
                            ),
                            Decimal(0),
                        ),
                    )
                ),
                "pending_buy_quantity": sum(
                    order.remaining
                    for order in orders
                    if order.signal.direction == "buy"
                ),
                "pair_ledger": tuple(dict(pair) for pair in pair_ledger),
            }
            generated = event_provider.signals_at(
                symbol, selected[: index + 1], decision_state
            )
            submitted_signals.extend(generated)
            bar_signals = (*bar_signals, *generated)
        for signal in bar_signals:
            if signal.intent == "buyback":
                eligible_pair = next(
                    (
                        item
                        for item in pair_ledger
                        if item["status"] == "open"
                        and (signal.pair_id is None or item["pair_id"] == signal.pair_id)
                    ),
                    None,
                )
                if eligible_pair is None:
                    continue
                signal = BarSignal(
                    signal.timestamp,
                    signal.direction,
                    signal.quantity,
                    signal.intent,
                    str(eligible_pair["pair_id"]),
                )
            requested = signal.quantity
            estimated_price = bar.close * (
                Decimal(1) + params.slippage_bps / BASIS_POINTS
            )
            if params.dynamic_sizing:
                nav = cash + holdings * bar.close
                reserved_cash = sum(
                    (order.reserved_cash for order in orders if order.signal.direction == "buy"),
                    Decimal(0),
                )
                pending_buy_quantity = sum(
                    order.remaining
                    for order in orders
                    if order.signal.direction == "buy"
                )
                if signal.intent is SignalIntent.BUYBACK:
                    pair = next(
                        (
                            item
                            for item in pair_ledger
                            if item["pair_id"] == signal.pair_id and item["status"] == "open"
                        ),
                        None,
                    )
                    pending_pair_quantity = sum(
                        order.remaining
                        for order in orders
                        if order.signal.intent is SignalIntent.BUYBACK
                        and order.signal.pair_id == signal.pair_id
                    )
                    requested = min(
                        signal.quantity,
                        max(0, int(pair["remaining_quantity"]) - pending_pair_quantity)
                        if pair is not None
                        else 0,
                    )
                    requested = _dynamic_buy_quantity(
                        requested=requested,
                        selected=selected,
                        index=index,
                        price=estimated_price,
                        nav=nav,
                        cash=cash,
                        holdings=holdings,
                        protected_floor=protected_floor,
                        reserved_cash=reserved_cash,
                        pending_buy_quantity=pending_buy_quantity,
                        params=params,
                    )
                elif signal.direction == "buy":
                    pending_additions = sum(
                        1
                        for order in orders
                        if order.signal.direction == "buy"
                        and order.signal.intent is not SignalIntent.BUYBACK
                    )
                    if daily_addition_count + pending_additions >= 3:
                        requested = 0
                    else:
                        requested = _dynamic_buy_quantity(
                            requested=None,
                            selected=selected,
                            index=index,
                            price=estimated_price,
                            nav=nav,
                            cash=cash,
                            holdings=holdings,
                            protected_floor=protected_floor,
                            reserved_cash=reserved_cash,
                            pending_buy_quantity=pending_buy_quantity,
                            params=params,
                        )
                else:
                    protected = (
                        0 if signal.intent is SignalIntent.RISK_EXIT else min(protected_floor, holdings)
                    )
                    sellable_above_floor = max(0, min(sellable, holdings - protected))
                    pending_ordinary_sells = sum(
                        (
                            order.remaining * estimated_price
                            for order in orders
                            if order.signal.direction == "sell"
                            and order.signal.intent is not SignalIntent.RISK_EXIT
                        ),
                        Decimal(0),
                    )
                    if signal.intent is SignalIntent.RISK_EXIT:
                        requested = min(signal.quantity, sellable_above_floor)
                    else:
                        daily_room = max(
                            Decimal(0),
                            Decimal("0.10") * nav
                            - daily_sell_notional
                            - pending_ordinary_sells,
                        )
                        requested = min(
                            signal.quantity,
                            sellable_above_floor,
                            int(
                                (daily_room / estimated_price).to_integral_value(
                                    rounding=ROUND_DOWN
                                )
                            )
                            // BOARD_LOT
                            * BOARD_LOT,
                        )
                if daily_fill_count >= 8 or daily_turnover >= Decimal("1.50") * nav:
                    requested = 0
                if daily_turnover + estimated_price * requested > Decimal("1.50") * nav:
                    requested = (
                        int(
                            (
                                max(Decimal(0), Decimal("1.50") * nav - daily_turnover)
                                / estimated_price
                            ).to_integral_value(rounding=ROUND_DOWN)
                        )
                        // BOARD_LOT
                        * BOARD_LOT
                    )
            if signal.direction == "buy":
                free_cash = max(Decimal(0), cash - sum(
                    (
                        order.reserved_cash
                        for order in orders
                        if order.signal.direction == "buy"
                    ),
                    Decimal(0),
                ))
                estimated_price = bar.close * (
                    Decimal(1) + params.slippage_bps / BASIS_POINTS
                )
                fee_reserve = params.minimum_commission * params.order_lifetime_bars
                order_budget = max(Decimal(0), free_cash - fee_reserve)
                unit_cost = estimated_price * (
                    Decimal(1) + params.fee_bps / BASIS_POINTS
                )
                reserved = min(free_cash, unit_cost * requested + fee_reserve)
                allowed = (
                    int(
                        (
                            min(order_budget, unit_cost * requested)
                            / (unit_cost * BOARD_LOT)
                        ).to_integral_value(rounding=ROUND_DOWN)
                    )
                    * BOARD_LOT
                )
                if allowed:
                    orders.append(_Order(signal, index, allowed, reserved))
            else:
                allowed = min(requested, sellable) // BOARD_LOT * BOARD_LOT
                if allowed:
                    sellable -= allowed
                    orders.append(_Order(signal, index, allowed, Decimal(0)))
        account_states.append(
            {
                "timestamp": bar.timestamp.isoformat(),
                "cash": str(cash),
                "holdings": holdings,
                "sellable": sellable,
                "protected_floor": min(protected_floor, holdings),
                "nav_at_close": str(cash + holdings * bar.close),
                "reserved_cash": str(
                    sum(
                        (order.reserved_cash for order in orders if order.signal.direction == "buy"),
                        Decimal(0),
                    )
                ),
                "pending_orders": [
                    {
                        "direction": order.signal.direction,
                        "intent": order.signal.intent,
                        "signal_timestamp": order.signal.timestamp.isoformat(),
                        "submitted_bar_index": order.submitted_index,
                        "quantity": order.remaining,
                        "reserved_cash": str(order.reserved_cash),
                        "pair_id": order.signal.pair_id,
                    }
                    for order in orders
                ],
                "daily_sell_notional": str(daily_sell_notional),
                "daily_turnover": str(daily_turnover),
                "daily_fill_count": daily_fill_count,
                "daily_addition_count": daily_addition_count,
                "pair_ledger": [dict(pair) for pair in pair_ledger],
            }
        )
    for order in orders:
        cancellations.append(
            {
                "signal_timestamp": order.signal.timestamp,
                "direction": order.signal.direction,
                "unfilled_quantity": order.remaining,
                "reserved_cash_released": order.reserved_cash,
            }
        )
    final_price = selected[-1].close
    strategy_value = cash + holdings * final_price
    baseline_value = initial_cash + initial_holdings * final_price
    initial_value = initial_cash + initial_holdings * selected[0].close
    if initial_value <= 0:
        raise ValueError("initial account value must be positive")
    snapshot_bars = tuple(_bar_dict(bar) for bar in selected)
    snapshot_json = json.dumps(snapshot_bars, sort_keys=True, separators=(",", ":"))
    initial_snapshot = {
        "as_of": account.as_of.isoformat(),
        "available_cash": str(account.available_cash),
        "holdings": dict(account.holdings),
        "cost_basis": {
            held_symbol: str(cost_basis)
            for held_symbol, cost_basis in account.cost_basis.items()
        },
        "sellable": dict(account.sellable),
    }
    assumptions = {
        "fill_timing": "next_available_bar_open",
        "signal_bar_close_fills": False,
        "participation_rate": params.participation_rate,
        "minimum_commission": params.minimum_commission,
        "fee_bps": params.fee_bps,
        "slippage_bps": params.slippage_bps,
        "order_lifetime_bars": params.order_lifetime_bars,
        "t_plus_one": "same-session buys are not sellable until a later session date",
        "dynamic_sizing": (
            {
                "total_position_nav_limit": "0.65",
                "cash_reserve_nav_limit": "0.35",
                "trading_position_nav_limit": "0.25",
                "single_order_nav_limit": "0.08",
                "risk_budget_nav_limit": "0.0015",
                "liquidity_participation_limit": "0.02",
                "grid_ratio_bounds": ["0.0025", "0.009"],
                "atr_grid_multiplier": "0.9",
                "stop_distance": "max(1.5*ATR14,1.2*grid,0.004*price)",
                "daily_addition_limit": 3,
                "daily_fill_limit": 8,
                "daily_turnover_nav_limit": "1.50",
                "daily_ordinary_sell_nav_limit": "0.10",
                "protected_floor": protected_floor,
                "limits_include_pending_orders": True,
                "risk_exit_may_bypass_protected_floor_and_daily_sell_limit": True,
            }
            if params.dynamic_sizing
            else None
        ),
        "submitted_signals": [
            {
                "timestamp": signal.timestamp.isoformat(),
                "direction": signal.direction,
                "quantity": signal.quantity,
                "intent": signal.intent,
                "pair_id": signal.pair_id,
            }
            for signal in submitted_signals
        ],
        "partial_fill": (
            "board-lot quantity limited by bar volume and the lower of configured "
            "participation or 2% in dynamic mode"
        ),
        "cancel": (
            "unfilled remainder canceled at order lifetime; reserved cash released"
        ),
        "baseline": (
            "initial cash and holdings held unchanged through final completed-bar close"
        ),
        "initial_account_snapshot": initial_snapshot,
    }
    return IntradayBacktestResult(
        status="completed",
        symbol=symbol,
        fills=tuple(fills),
        cancellations=tuple(cancellations),
        snapshot_id=hashlib.sha256(snapshot_json.encode()).hexdigest(),
        snapshot_json=snapshot_json,
        snapshot_bar_count=len(selected),
        implementation_version=implementation_version,
        strategy_parameters=dict(strategy_parameters),
        pair_ledger=tuple(dict(pair) for pair in pair_ledger),
        account_states=tuple(account_states),
        assumptions=assumptions,
        initial_cash=initial_cash,
        initial_holdings=initial_holdings,
        final_cash=cash,
        final_holdings=holdings,
        strategy_final_value=strategy_value,
        baseline_final_value=baseline_value,
        strategy_return=(strategy_value - initial_value) / initial_value,
        baseline_return=(baseline_value - initial_value) / initial_value,
    )


def _bar_dict(bar: MinuteBar) -> dict[str, object]:
    return {
        "symbol": bar.symbol,
        "timestamp": bar.timestamp.isoformat(),
        "interval_minutes": bar.interval_minutes,
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": bar.volume,
        "is_completed": bar.is_completed,
    }
def _risk_quantity(
    bars: tuple[MinuteBar, ...],
    index: int,
    price: Decimal,
    nav: Decimal,
    params: IntradayBacktestParams,
) -> int | None:
    """Apply the V1 ATR/grid stop distance and 0.15% NAV risk budget."""
    if index < 13 or price <= 0 or nav <= 0:
        return None
    ranges: list[Decimal] = []
    for bar_index in range(index + 1):
        current = bars[bar_index]
        previous_close = bars[bar_index - 1].close if bar_index else current.close
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous_close),
                abs(current.low - previous_close),
            )
        )
    atr = sum(ranges[:14], Decimal(0)) / Decimal(14)
    for true_range in ranges[14:]:
        atr = (atr * Decimal(13) + true_range) / Decimal(14)
    if atr <= 0:
        return None
    quantity = (
        Decimal("0.0015") * nav / max(Decimal("1.5") * atr, Decimal("0.004") * price)
    ).to_integral_value(rounding=ROUND_DOWN)
    quantity = quantity // BOARD_LOT * BOARD_LOT
    if quantity < BOARD_LOT:
        return 0
    round_trip_cost_rate = Decimal(0)
    grid = Decimal(0)
    for _ in range(8):
        notional = price * quantity
        round_trip_cost_rate = (
            Decimal(2)
            * max(
                params.minimum_commission,
                notional * params.fee_bps / BASIS_POINTS,
            )
            / notional
            + Decimal("0.0004")
        )
        grid_ratio = max(
            Decimal("0.9") * atr / price,
            Decimal("0.0025"),
            Decimal(3) * round_trip_cost_rate,
            Decimal("0.004") / price,
        )
        if grid_ratio > Decimal("0.009"):
            return None
        grid = (price * grid_ratio / Decimal("0.001")).to_integral_value(
            rounding=ROUND_UP
        ) * Decimal("0.001")
        stop_distance = max(
            Decimal("1.5") * atr,
            Decimal("1.2") * grid,
            Decimal("0.004") * price,
        )
        next_quantity = (
            Decimal("0.0015") * nav / stop_distance
        ).to_integral_value(rounding=ROUND_DOWN)
        next_quantity = max(
            Decimal(BOARD_LOT), next_quantity // BOARD_LOT * BOARD_LOT
        )
        if next_quantity == quantity:
            break
        quantity = next_quantity
    return max(0, int(quantity))


def _dynamic_buy_quantity(
    *,
    requested: int | None,
    selected: tuple[MinuteBar, ...],
    index: int,
    price: Decimal,
    nav: Decimal,
    cash: Decimal,
    holdings: int,
    protected_floor: int,
    reserved_cash: Decimal,
    pending_buy_quantity: int,
    params: IntradayBacktestParams,
) -> int:
    risk_quantity = _risk_quantity(selected, index, price, nav, params)
    if risk_quantity is None:
        return 0
    cash_room = max(Decimal(0), cash - Decimal("0.35") * nav - reserved_cash)
    position_room = max(
        Decimal(0),
        Decimal("0.65") * nav
        - holdings * price
        - Decimal(pending_buy_quantity) * price,
    )
    trading_position = max(0, holdings - min(protected_floor, holdings)) * price
    trading_room = max(
        Decimal(0),
        Decimal("0.25") * nav
        - trading_position
        - Decimal(pending_buy_quantity) * price,
    )
    liquidity_room = max(
        Decimal(0),
        Decimal(max(0, selected[index].volume)) * Decimal("0.02")
        - Decimal(pending_buy_quantity),
    )
    limits = [
        Decimal(risk_quantity),
        cash_room / price,
        position_room / price,
        trading_room / price,
        Decimal("0.08") * nav / price,
        liquidity_room,
    ]
    if requested is not None:
        limits.append(Decimal(requested))
    capacity = min(limits)
    return (
        int(capacity.to_integral_value(rounding=ROUND_DOWN))
        // BOARD_LOT
        * BOARD_LOT
    )
