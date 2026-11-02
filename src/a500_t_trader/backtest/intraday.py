"""Signal-driven, auditable intraday bar execution simulation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
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


@dataclass(frozen=True, slots=True)
class BarSignal:
    timestamp: datetime
    direction: str
    quantity: int

    def __post_init__(self) -> None:
        if self.direction not in ("buy", "sell"):
            raise ValueError("signal direction must be buy or sell")
        if self.quantity <= 0 or self.quantity % BOARD_LOT:
            raise ValueError("signal quantity must be a positive board lot")


class StrategySignalProvider(Protocol):
    def signals(
        self, symbol: str, bars: tuple[MinuteBar, ...]
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
    never reach it, and each candidate uses only bars available at that time.
    """

    def __init__(
        self,
        *,
        quantity_for: CandidateQuantityResolver,
        config: IntradayV1Config | None = None,
        grid_context: Mapping[str, Mapping[str, object]] | None = None,
        grid_context_for: TechnicalGridContextProvider | None = None,
    ) -> None:
        self._quantity_for = quantity_for
        self._config = config
        self._grid_context = grid_context
        self._grid_context_for = grid_context_for

    def signals(
        self, symbol: str, bars: tuple[MinuteBar, ...]
    ) -> tuple[BarSignal, ...]:
        signals: list[BarSignal] = []
        history: list[MinuteBar] = []
        for bar in bars:
            if not bar.is_completed:
                continue
            history.append(bar)
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
            )[symbol]
            direction = analysis.candidate.lower()
            if analysis.risk_status != "READY" or direction not in {"buy", "sell"}:
                continue
            quantity = self._quantity_for.quantity_for(symbol, analysis, history_tuple)
            if quantity is not None:
                signals.append(BarSignal(bar.timestamp, direction, quantity))
        return tuple(signals)


@dataclass(frozen=True, slots=True)
class IntradayBacktestParams:
    participation_rate: Decimal = Decimal("0.1")
    fee_bps: Decimal = Decimal(0)
    slippage_bps: Decimal = Decimal(0)
    order_lifetime_bars: int = 1

    def __post_init__(self) -> None:
        if not Decimal(0) < self.participation_rate <= Decimal(1):
            raise ValueError("participation_rate must be in (0, 1]")
        if self.fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError("fees and slippage cannot be negative")
        if self.order_lifetime_bars < 1:
            raise ValueError("order_lifetime_bars must be positive")


@dataclass(frozen=True, slots=True)
class IntradayFill:
    side: str
    quantity: int
    signal_timestamp: datetime
    fill_timestamp: datetime
    fill_price: Decimal
    fees: Decimal
    slippage: Decimal


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
    signals: tuple[BarSignal, ...] | StrategySignalProvider | None,
    params: IntradayBacktestParams,
    strategy_parameters: dict[str, object],
    implementation_version: str,
) -> IntradayBacktestResult:
    """Simulate supplied strategy signals; no strategy scoring is inferred here."""
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
    if not isinstance(signals, tuple):
        signals = signals.signals(symbol, selected)
    times = {bar.timestamp for bar in selected}
    if any(signal.timestamp not in times for signal in signals):
        raise ValueError("every signal must refer to a completed input bar")
    if tuple(sorted(signals, key=lambda signal: signal.timestamp)) != signals:
        raise ValueError("signals must be in timestamp order")

    signal_at: dict[datetime, list[BarSignal]] = {}
    for signal in signals:
        signal_at.setdefault(signal.timestamp, []).append(signal)
    initial_cash = account.available_cash
    initial_holdings = account.holdings.get(symbol, 0)
    cash = initial_cash
    holdings = initial_holdings
    sellable = min(initial_holdings, account.sellable.get(symbol, 0))
    acquired: list[tuple[date, int]] = []
    orders: list[_Order] = []
    fills: list[IntradayFill] = []
    cancellations: list[dict[str, object]] = []
    for index, bar in enumerate(selected):
        day = bar.timestamp.date()
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
                int(Decimal(bar.volume) * params.participation_rate)
                // BOARD_LOT
                * BOARD_LOT
            )
            for order in tuple(orders):
                if index <= order.submitted_index:
                    continue
                quantity = min(order.remaining, max_fill)
                if order.signal.direction == "buy" and quantity:
                    execution_price = bar.open * (
                        Decimal(1) + params.slippage_bps / BASIS_POINTS
                    )
                    unit_cost = execution_price * (
                        Decimal(1) + params.fee_bps / BASIS_POINTS
                    )
                    reserved_for_order = order.reserved_cash
                    max_affordable = (
                        int(
                            (
                                min(cash, reserved_for_order, unit_cost * quantity)
                                / (unit_cost * BOARD_LOT)
                            ).to_integral_value(rounding=ROUND_DOWN)
                        )
                        * BOARD_LOT
                    )
                    quantity = min(quantity, max_affordable)
                if quantity:
                    if order.signal.direction == "buy":
                        fill_price = bar.open * (
                            Decimal(1) + params.slippage_bps / BASIS_POINTS
                        )
                        notional = fill_price * quantity
                        fees = notional * params.fee_bps / BASIS_POINTS
                        slippage = (fill_price - bar.open) * quantity
                        cash -= notional + fees
                        holdings += quantity
                        acquired.append((day, quantity))
                        order.reserved_cash = max(
                            Decimal(0), order.reserved_cash - notional - fees
                        )
                    else:
                        fill_price = bar.open * (
                            Decimal(1) - params.slippage_bps / BASIS_POINTS
                        )
                        notional = fill_price * quantity
                        fees = notional * params.fee_bps / BASIS_POINTS
                        slippage = (bar.open - fill_price) * quantity
                        cash += notional - fees
                        holdings -= quantity
                        # Sellable inventory was reserved when the order was accepted.
                    fills.append(
                        IntradayFill(
                            order.signal.direction,
                            quantity,
                            order.signal.timestamp,
                            bar.timestamp,
                            fill_price,
                            fees,
                            slippage,
                        )
                    )
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
        for signal in signal_at.get(bar.timestamp, ()):
            if signal.direction == "buy":
                free_cash = max(
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
                estimated_unit_cost = (
                    bar.close
                    * (Decimal(1) + params.slippage_bps / BASIS_POINTS)
                    * (Decimal(1) + params.fee_bps / BASIS_POINTS)
                )
                reserved = min(free_cash, estimated_unit_cost * signal.quantity)
                allowed = (
                    int(
                        (
                            reserved / (estimated_unit_cost * BOARD_LOT)
                        ).to_integral_value(rounding=ROUND_DOWN)
                    )
                    * BOARD_LOT
                )
                if allowed:
                    orders.append(_Order(signal, index, allowed, reserved))
            else:
                allowed = min(signal.quantity, sellable) // BOARD_LOT * BOARD_LOT
                if allowed:
                    sellable -= allowed
                    orders.append(_Order(signal, index, allowed, Decimal(0)))
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
        "fee_bps": params.fee_bps,
        "slippage_bps": params.slippage_bps,
        "order_lifetime_bars": params.order_lifetime_bars,
        "t_plus_one": "same-session buys are not sellable until a later session date",
        "partial_fill": (
            "board-lot quantity limited by configured participation rate and bar volume"
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
        strategy_parameters=dict(strategy_parameters),
        implementation_version=implementation_version,
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
