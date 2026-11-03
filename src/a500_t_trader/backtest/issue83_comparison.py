"""Reproducible exploratory Issue 83 source-rule comparison."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, time
from decimal import Decimal
from typing import Any

from ..market_data import SHANGHAI, MinuteBar
from ..strategy.intraday_technical import (
    TECHNICAL_PARAMETERS,
    TECHNICAL_PARAMETER_HASH,
)
from ..strategy.intraday_v1_config import RULE_SOURCE_SHA256, STRATEGY_ID
from ..strategy.premarket import AccountSnapshot
from .intraday import (
    IntradayBacktestParams,
    TechnicalAnalysisSignalProvider,
    TechnicalGridContextProvider,
    run_intraday_backtest,
)

SYMBOL = "563360"
START = datetime(2024, 10, 18, 14, 0, tzinfo=SHANGHAI)
END = datetime(2026, 10, 28, 15, 0, tzinfo=SHANGHAI)
WARMUP_START = datetime(2024, 10, 15, 0, 0, tzinfo=SHANGHAI)
INITIAL_ACCOUNT_TIME = datetime(2024, 10, 18, 13, 55, tzinfo=SHANGHAI)
IMPLEMENTATION_VERSION = "issue83-ruleset-account-comparison-1"


def _average_daily_amount(bars: tuple[MinuteBar, ...]) -> Decimal | None:
    """Return the trailing 20 complete 48-bar session amounts, if all are valid."""
    daily: dict[object, list[MinuteBar]] = {}
    for bar in bars:
        day = bar.timestamp.astimezone(SHANGHAI).date()
        daily.setdefault(day, []).append(bar)
    sessions: list[tuple[object, Decimal]] = []
    for day, session in daily.items():
        local_times = {
            bar.timestamp.astimezone(SHANGHAI).time() for bar in session
        }
        if (
            len(session) != 48
            or len(local_times) != 48
            or max(local_times) != time(14, 55)
            or any(bar.amount is None for bar in session)
        ):
            continue
        sessions.append(
            (day, sum((bar.amount for bar in session), Decimal(0)))
        )
    sessions.sort(key=lambda item: item[0])
    if len(sessions) < 20:
        return None
    return sum((amount for _, amount in sessions[-20:]), Decimal(0)) / 20


def _round_trip_cost_rate(
    price: Decimal,
    quantity: int,
    *,
    fee_rate: Decimal = Decimal("0.00005"),
    minimum_commission: Decimal = Decimal("0.1"),
    single_side_impact: Decimal = Decimal("0.0002"),
) -> Decimal:
    notional = price * quantity
    if price <= 0 or quantity <= 0:
        return Decimal("Infinity")
    fees_each_side = max(minimum_commission, notional * fee_rate)
    return fees_each_side * 2 / notional + single_side_impact * 2

class _SequentialV1Signals:
    """Stateful V1 policy evaluated after each completed bar's prior fills."""

    def __init__(
        self,
        warmup: tuple[MinuteBar, ...],
        pre_start: tuple[MinuteBar, ...],
        threshold_overrides: dict[str, int] | None = None,
        fee_rate: Decimal = Decimal("0.00005"),
        minimum_commission: Decimal = Decimal("0.1"),
        impact_rate: Decimal = Decimal("0.0002"),
    ) -> None:
        self._threshold_overrides = threshold_overrides
        self._fee_rate = fee_rate
        self._minimum_commission = minimum_commission
        self._impact_rate = impact_rate
        self._history = list(warmup[-180:])
        self._grid_date = None
        self._used_levels: set[str] = set()
        self._current_step: Decimal | None = None
        self._lots: list[dict[str, object]] = []
        self._cost_grid_examples: list[dict[str, object]] = []
        self._cost_grid_example_keys: set[int] = set()
        self._seen_fill_count = 0
        self._last_fill_by_side: dict[str, dict[str, object]] = {}
        self._daily_start_nav: Decimal | None = None
        self._day = pre_start[-1].timestamp.astimezone(SHANGHAI).date() if pre_start else None
        self._day_amount = Decimal(0)
        daily_bars: dict[object, list[MinuteBar]] = {}
        for prior in pre_start:
            day = prior.timestamp.astimezone(SHANGHAI).date()
            daily_bars.setdefault(day, []).append(prior)
        self._completed_daily_amounts = []
        for day, session in sorted(daily_bars.items()):
            if day == self._day:
                continue
            local_times = {
                bar.timestamp.astimezone(SHANGHAI).time() for bar in session
            }
            if (
                len(session) == 48
                and len(local_times) == 48
                and max(local_times) == time(14, 55)
                and all(bar.amount is not None for bar in session)
            ):
                self._completed_daily_amounts.append(
                    sum((bar.amount for bar in session), Decimal(0))
                )
        if self._day is not None:
            self._day_amount = sum(
                (
                    bar.amount
                    for bar in daily_bars[self._day]
                    if bar.amount is not None
                ),
                Decimal(0),
            )
        self._last_close: Decimal | None = None
        self._opposite_anchor_closes = 0
        self._anchor_side: int | None = None
        self._entry_pause_until = -1

    def _apply_new_fills(self, state: dict[str, object]) -> None:
        fills = state["fills"]
        assert isinstance(fills, tuple)
        for fill in fills[self._seen_fill_count :]:
            side = str(fill["side"])
            quantity = int(fill["quantity"])
            if side == "buy":
                self._lots.append(
                    {
                        "quantity": quantity,
                        "entry_price": Decimal(str(fill["fill_price"])),
                        "atr": Decimal(str(fill["atr"] or "0")),
                        "grid_step": Decimal(str(fill["grid_step"] or "0")),
                        "highest_close": Decimal(str(fill["fill_price"])),
                        "grid_level": fill["grid_level"],
                    }
                )
                if fill["grid_level"]:
                    self._used_levels.add(str(fill["grid_level"]))
            else:
                remaining = quantity
                for lot in self._lots:
                    available = int(lot["quantity"])
                    used = min(available, remaining)
                    lot["quantity"] = available - used
                    remaining -= used
                    if remaining == 0:
                        break
                self._lots = [lot for lot in self._lots if int(lot["quantity"]) > 0]
                if fill["grid_level"]:
                    self._used_levels.add(str(fill["grid_level"]))
            self._last_fill_by_side[side] = dict(fill)
        self._seen_fill_count = len(fills)

    def _ordinary_quantity(
        self, nav: Decimal, price: Decimal, atr: Decimal, grid: Decimal
    ) -> int:
        risk_distance = max(
            Decimal("1.5") * atr,
            Decimal("1.2") * grid,
            Decimal("0.004") * price,
        )
        quantity = min(
            Decimal("0.0015") * nav / risk_distance,
            Decimal("0.08") * nav / price,
            Decimal("0.25") * nav / price,
        )
        return int(quantity // 100) * 100

    def _liquid(self, bar: MinuteBar) -> bool:
        trailing = self._completed_daily_amounts[-20:]
        return (
            len(trailing) == 20
            and sum(trailing, Decimal(0)) / 20 >= Decimal("30000000")
            and bar.amount is not None
            and bar.amount >= Decimal("1000000")
        )

    def signals_at(
        self,
        symbol: str,
        bars: tuple[MinuteBar, ...],
        account_state: dict[str, object],
    ) -> tuple[object, ...]:
        from ..strategy.intraday_api import _filter_rsi_momentum
        from ..strategy.intraday_technical import analyze_intraday_technical
        from .intraday import BarSignal, SignalIntent

        bar = bars[-1]
        session_date = bar.timestamp.astimezone(SHANGHAI).date()
        nav = Decimal(str(account_state["nav"]))
        price = bar.close
        if self._day is None or session_date != self._day:
            if self._day is not None:
                self._completed_daily_amounts.append(self._day_amount)
            self._day = session_date
            self._day_amount = Decimal(0)
            self._daily_start_nav = nav
            self._used_levels.clear()
            self._current_step = None
            self._opposite_anchor_closes = 0
            self._anchor_side = None
        elif self._daily_start_nav is None:
            self._daily_start_nav = nav
        self._grid_date = session_date
        if bar.amount is not None:
            self._day_amount += bar.amount
        self._apply_new_fills(account_state)
        history = tuple((*self._history, bar)[-181:])
        current_index = int(account_state["bar_index"])
        tick = Decimal("0.001")
        cost_rate = Decimal(0)
        pending = bool(account_state["pending_orders"])
        open_pairs = [
            pair
            for pair in account_state["pair_ledger"]
            if pair["status"] == "open" and int(pair["remaining_quantity"]) > 0
        ]
        active_lots = [lot for lot in self._lots if int(lot["quantity"]) > 0]

        def analyze_at_cost(rate: Decimal):
            paired_state: dict[str, object] = {}
            if open_pairs:
                pair = open_pairs[0]
                sell_price = Decimal(str(pair["sell_price"]))
                frozen_step = Decimal(str(pair["sell_grid_step"] or "0"))
                paired_state["buyback"] = {
                    "target_price": sell_price
                    - max(frozen_step, Decimal(3) * rate * sell_price),
                    "quantity": int(pair["remaining_quantity"]),
                    "trade_pair_id": pair["pair_id"],
                }
            if active_lots:
                lot = active_lots[0]
                entry = Decimal(str(lot["entry_price"]))
                lot_grid = Decimal(str(lot["grid_step"]))
                paired_state["take_profit"] = {
                    "target_price": entry + max(lot_grid, Decimal(3) * rate * entry),
                    "quantity": int(lot["quantity"]),
                }
            grid_context = {
                "tick_size": tick,
                "round_trip_cost_rate": rate,
                "grid_date": self._grid_date,
                "current_step": self._current_step,
                "used_levels": tuple(self._used_levels),
                "pending_grid_orders": pending,
            }
            result = analyze_intraday_technical(
                {symbol: history},
                grid_context={symbol: grid_context},
                paired_state={symbol: paired_state} if paired_state else {},
                as_of=bar.timestamp,
                score_thresholds=self._threshold_overrides,
            )[symbol]
            return _filter_rsi_momentum(result)

        analysis = analyze_at_cost(cost_rate)
        conservative_rate = cost_rate
        converged = False
        previous_candidate: str | None = None
        previous_quantity = -1
        for _ in range(8):
            candidate = analysis.candidate
            grid_step = analysis.grid_step or Decimal(0)
            atr_value = analysis.indicators.get("atr_14")
            atr_value = atr_value if isinstance(atr_value, Decimal) else Decimal(0)
            quantity = 0
            if candidate in {"BUY", "BUYBACK"} and not pending:
                quantity = self._ordinary_quantity(nav, price, atr_value, grid_step)
                if candidate == "BUYBACK" and open_pairs:
                    pair = open_pairs[0]
                    pair_limit = int(pair["remaining_quantity"])
                    if analysis.trend == "BEAR":
                        pair_limit = min(pair_limit, int(pair["sell_quantity"]) // 4)
                    quantity = min(quantity, pair_limit)
                elif candidate == "BUY":
                    free_cash = Decimal(str(account_state["free_cash"]))
                    cash_per_share = price * (
                        Decimal(1) + self._fee_rate + self._impact_rate
                    ) + self._minimum_commission / Decimal(100)
                    quantity = min(
                        quantity,
                        int((free_cash / cash_per_share) // 100) * 100,
                    )
                    if (
                        int(account_state["daily_addition_count"]) >= 3
                        or int(account_state["remaining_daily_additions"]) <= 0
                    ):
                        quantity = 0
                elif candidate == "BUYBACK":
                    free_cash = Decimal(str(account_state["free_cash"]))
                    cash_per_share = price * (
                        Decimal(1) + self._fee_rate + self._impact_rate
                    ) + self._minimum_commission / Decimal(100)
                    quantity = min(
                        quantity,
                        int((free_cash / cash_per_share) // 100) * 100,
                    )
                quantity = min(
                    quantity,
                    int(Decimal(str(account_state["remaining_daily_turnover"])) / price)
                    // 100
                    * 100,
                )
                if int(account_state["remaining_daily_fills"]) <= 0:
                    quantity = 0
            elif candidate == "SELL" and not pending:
                holdings = int(account_state["holdings"])
                sellable = int(account_state["sellable"])
                floor = int((nav * Decimal("0.30") / price) // 100) * 100
                quantity = min(
                    max(0, min(sellable, holdings - floor)),
                    self._ordinary_quantity(nav, price, atr_value, grid_step),
                    int(
                        Decimal(str(account_state["remaining_daily_sell_notional"]))
                        / price
                    )
                    // 100
                    * 100,
                )
                if int(account_state["remaining_daily_fills"]) <= 0:
                    quantity = 0
            elif candidate == "TAKE_PROFIT" and active_lots and not pending:
                quantity = min(
                    int(active_lots[0]["quantity"]),
                    int(account_state["sellable"]),
                    int(
                        Decimal(str(account_state["remaining_daily_sell_notional"]))
                        / price
                    ),
                )
            next_rate = max(
                conservative_rate,
                _round_trip_cost_rate(
                    price,
                    quantity,
                    fee_rate=self._fee_rate,
                    minimum_commission=self._minimum_commission,
                    single_side_impact=self._impact_rate,
                )
                if quantity >= 100
                else Decimal(0),
            )
            conservative_rate = next_rate
            if candidate == previous_candidate and quantity == previous_quantity:
                cost_rate = next_rate
                converged = True
                break
            previous_candidate, previous_quantity = candidate, quantity
            cost_rate = next_rate
            analysis = analyze_at_cost(cost_rate)
        if not converged:
            cost_rate = conservative_rate
            analysis = analyze_at_cost(cost_rate)
        self._history.append(bar)
        indicators = analysis.indicators
        atr = indicators.get("atr_14")
        grid = analysis.grid_step
        anchor = indicators.get("grid_anchor")
        if anchor is not None:
            side = 1 if price > anchor else -1 if price < anchor else 0
            if self._anchor_side is not None and side and side != self._anchor_side:
                self._opposite_anchor_closes += 1
            elif side:
                self._opposite_anchor_closes = 0
            if self._opposite_anchor_closes >= 2:
                self._used_levels.clear()
                self._opposite_anchor_closes = 0
            if side:
                self._anchor_side = side
        self._current_step = grid
        if atr is not None and self._last_close is not None and atr > 0:
            if abs(price - self._last_close) > Decimal("2.5") * atr:
                self._entry_pause_until = current_index + 2
        self._last_close = price

        sellable = int(account_state["sellable"])
        holdings = int(account_state["holdings"])
        start_nav = self._daily_start_nav or nav
        daily_return = (nav - start_nav) / start_nav if start_nav else Decimal(0)
        risk_exit_quantity = 0
        for lot in active_lots:
            lot["highest_close"] = max(Decimal(str(lot["highest_close"])), price)
            entry = Decimal(str(lot["entry_price"]))
            lot_atr = Decimal(str(lot["atr"]))
            lot_grid = Decimal(str(lot["grid_step"]))
            initial_stop = entry - max(
                Decimal("1.5") * lot_atr,
                Decimal("1.2") * lot_grid,
                Decimal("0.004") * entry,
            )
            stop = initial_stop
            if Decimal(str(lot["highest_close"])) >= entry + lot_grid:
                stop = max(
                    stop,
                    Decimal(str(lot["highest_close"]))
                    - max(Decimal("1.2") * lot_atr, lot_grid),
                    entry + Decimal(3) * cost_rate * entry,
                )
            if price <= stop:
                risk_exit_quantity += int(lot["quantity"])
        if (daily_return <= Decimal("-0.012") or risk_exit_quantity) and sellable:
            quantity = min(sellable, holdings, risk_exit_quantity or sellable)
            quantity = quantity // 100 * 100
            if quantity:
                return (
                    BarSignal(
                        bar.timestamp,
                        "sell",
                        quantity,
                        SignalIntent.RISK_EXIT,
                        atr=atr,
                        grid_step=grid,
                    ),
                )
        if analysis.risk_status != "READY":
            return ()
        if pending:
            return ()
        candidate = analysis.candidate
        if daily_return <= Decimal("-0.008") or current_index <= self._entry_pause_until:
            if candidate in {"BUY", "BUYBACK"}:
                return ()
        side = "buy" if candidate in {"BUY", "BUYBACK"} else "sell"
        last_fill = self._last_fill_by_side.get(side)
        if candidate in {"BUY", "BUYBACK", "SELL", "TAKE_PROFIT"} and last_fill:
            elapsed = bar.timestamp - datetime.fromisoformat(
                str(last_fill["fill_timestamp"])
            )
            if elapsed.total_seconds() < 600:
                return ()
        if candidate == "BUYBACK" and open_pairs:
            pair = open_pairs[0]
            pair_limit = int(pair["remaining_quantity"])
            if analysis.trend == "BEAR":
                pair_limit = min(pair_limit, int(pair["sell_quantity"]) // 4)
            cash_per_share = price * (
                Decimal(1) + self._fee_rate + self._impact_rate
            ) + self._minimum_commission / Decimal(100)
            quantity = min(
                pair_limit,
                self._ordinary_quantity(nav, price, atr or Decimal(0), grid or Decimal(0)),
                int(Decimal(str(account_state["free_cash"])) / cash_per_share)
                // 100
                * 100,
                int(Decimal(str(account_state["remaining_daily_turnover"])) / price)
                // 100
                * 100,
            )
            quantity = quantity // 100 * 100
            if (
                quantity >= 100
                and int(account_state["remaining_daily_fills"]) > 0
                and daily_return > Decimal("-0.008")
            ):
                return (
                    BarSignal(
                        bar.timestamp,
                        "buy",
                        quantity,
                        SignalIntent.BUYBACK,
                        str(pair["pair_id"]),
                        atr,
                        grid,
                    ),
                )
            return ()
        if candidate == "TAKE_PROFIT" and active_lots and sellable:
            lot = active_lots[0]
            quantity = min(
                int(lot["quantity"]),
                sellable,
                int(
                    Decimal(str(account_state["remaining_daily_sell_notional"]))
                    / price
                ),
            ) // 100 * 100
            if quantity:
                return (
                    BarSignal(
                        bar.timestamp,
                        "sell",
                        quantity,
                        SignalIntent.TAKE_PROFIT,
                        atr=atr,
                        grid_step=grid,
                    ),
                )
            return ()
        if not self._liquid(bar) or grid is None or atr is None:
            return ()
        if candidate == "BUY":
            if (
                int(account_state["daily_addition_count"]) >= 3
                or int(account_state["remaining_daily_additions"]) <= 0
                or int(account_state["remaining_daily_fills"]) <= 0
            ):
                return ()
            cash_per_share = price * (
                Decimal(1) + self._fee_rate + self._impact_rate
            ) + self._minimum_commission / Decimal(100)
            quantity = min(
                self._ordinary_quantity(nav, price, atr, grid),
                int(
                    Decimal(str(account_state["free_cash"])) / cash_per_share
                )
                // 100
                * 100,
                int(Decimal(str(account_state["remaining_daily_turnover"])) / price)
                // 100
                * 100,
            )
            if quantity < 100:
                return ()
            if quantity not in self._cost_grid_example_keys:
                self._cost_grid_example_keys.add(quantity)
                if len(self._cost_grid_examples) < 12:
                    quantity_cost_rate = _round_trip_cost_rate(
                        price,
                        quantity,
                        fee_rate=self._fee_rate,
                        minimum_commission=self._minimum_commission,
                        single_side_impact=self._impact_rate,
                    )
                    self._cost_grid_examples.append(
                        {
                            "candidate": candidate,
                            "quantity": quantity,
                            "price": str(price),
                            "quantity_cost_rate": str(quantity_cost_rate),
                            "cost_rate": str(cost_rate),
                            "grid_step": str(grid),
                            "eligible": True,
                        }
                    )
            level = indicators.get("grid_buy_level")
            return (
                BarSignal(
                    bar.timestamp,
                    "buy",
                    quantity,
                    atr=atr,
                    grid_step=grid,
                    grid_level=str(level) if level is not None else None,
                ),
            )
        if candidate == "SELL" and sellable > 0:
            if int(account_state["remaining_daily_fills"]) <= 0:
                return ()
            base_floor = int((nav * Decimal("0.30") / price) // 100) * 100
            reducible = max(0, holdings - base_floor)
            quantity = min(
                reducible,
                sellable,
                self._ordinary_quantity(nav, price, atr, grid),
                int(
                    Decimal(str(account_state["remaining_daily_sell_notional"]))
                    / price
                ),
            )
            quantity = quantity // 100 * 100
            if quantity >= 100:
                level = indicators.get("grid_sell_level")
                return (
                    BarSignal(
                        bar.timestamp,
                        "sell",
                        quantity,
                        atr=atr,
                        grid_step=grid,
                        grid_level=str(level) if level is not None else None,
                    ),
                )
        return ()


class _OneBoardLot:
    def quantity_for(
        self,
        symbol: str,
        analysis: IntradayTechnicalAnalysis,
        bars: tuple[MinuteBar, ...],
    ) -> int:
        return 100


class _ExploratoryGridContext(TechnicalGridContextProvider):
    def context_for(
        self, symbol: str, bars: tuple[MinuteBar, ...]
    ) -> dict[str, object]:
        # Only the stable fee/tick inputs are supplied; the source analyzer derives
        # a causal session grid from the bars and does not reuse any future grid state.
        return {
            "tick_size": Decimal("0.001"),
            "round_trip_cost_rate": Decimal("0.0001"),
        }


def run_issue83_comparison(bars: tuple[MinuteBar, ...]) -> dict[str, object]:
    """Run the three source-scored groups on one fixed account and execution model."""
    if not bars:
        raise ValueError("market history is empty")
    if any(bar.symbol != SYMBOL for bar in bars):
        raise ValueError(f"market history must contain only {SYMBOL}")
    ordered = tuple(
        sorted(
            (bar for bar in bars if bar.is_completed),
            key=lambda bar: bar.timestamp,
        )
    )
    all_warmup_bars = tuple(
        bar for bar in ordered if WARMUP_START <= bar.timestamp < START
    )
    warmup_bars = all_warmup_bars[-180:]
    decision_bars = tuple(bar for bar in ordered if START <= bar.timestamp <= END)
    relevant = (*warmup_bars, *decision_bars)
    if len(warmup_bars) < 180:
        raise ValueError(
            f"Issue 83 requires 180 completed warm-up bars; found "
            f"{len(all_warmup_bars)}"
        )
    if len(decision_bars) < 2:
        raise ValueError("market history must contain at least two decision bars")
    if decision_bars[0].timestamp != START:
        raise ValueError(
            f"first decision bar must be {START.isoformat()}; "
            f"found {decision_bars[0].timestamp.isoformat()}"
        )
    account = AccountSnapshot(
        as_of=INITIAL_ACCOUNT_TIME,
        holdings={},
        cost_basis={},
        sellable={},
        available_cash=Decimal("500000"),
    )
    execution = IntradayBacktestParams(
        participation_rate=Decimal("0.02"),
        fee_bps=Decimal("0.5"),
        slippage_bps=Decimal(0),
        order_lifetime_bars=1,
        minimum_commission=Decimal("0.1"),
    )
    group_specs = (
        (
            "kdj15_rsi0",
            {"ema": 20, "bollinger": 20, "kdj": 15, "rsi": 0, "vwap": 25},
            False,
        ),
        (
            "kdj8_rsi7",
            {"ema": 20, "bollinger": 20, "kdj": 8, "rsi": 7, "vwap": 25},
            False,
        ),
        (
            "kdj8_rsi7_rsi_momentum",
            {"ema": 20, "bollinger": 20, "kdj": 8, "rsi": 7, "vwap": 25},
            True,
        ),
    )
    groups: dict[str, dict[str, Any]] = {}
    for name, weights, momentum_filter in group_specs:
        provider = TechnicalAnalysisSignalProvider(
            quantity_for=_OneBoardLot(),
            grid_context_for=_ExploratoryGridContext(),
            max_history_bars=181,
            score_weights=weights,
            rsi_momentum_filter=momentum_filter,
        )
        candidate_signals = provider.signals(SYMBOL, relevant)
        signals = tuple(
            signal for signal in candidate_signals if START <= signal.timestamp <= END
        )
        simulation = run_intraday_backtest(
            symbol=SYMBOL,
            account=account,
            bars=decision_bars,
            signals=signals,
            params=execution,
            strategy_parameters={
                "group": name,
                "score_weights": weights,
                "rsi_momentum_filter": momentum_filter,
                "quantity_policy": "one fixed 100-share board lot per candidate",
                "maximum_history_bars": 181,
            },
            implementation_version=IMPLEMENTATION_VERSION,
        )
        weight_hash = _score_weight_hash(weights)
        groups[name] = {
            "analysis_parameter_hash": weight_hash,
            "score_weights": weights,
            "rsi_momentum_filter": momentum_filter,
            "signal_count": len(signals),
            "signals": [
                {
                    "timestamp": signal.timestamp.isoformat(),
                    "direction": signal.direction,
                    "quantity": signal.quantity,
                }
                for signal in signals
            ],
            "fill_count": len(simulation.fills),
            "fills": [
                {
                    "side": fill.side,
                    "quantity": fill.quantity,
                    "signal_timestamp": fill.signal_timestamp.isoformat(),
                    "fill_timestamp": fill.fill_timestamp.isoformat(),
                    "fill_price": str(fill.fill_price),
                    "fees": str(fill.fees),
                    "slippage": str(fill.slippage),
                }
                for fill in simulation.fills
            ],
            "cancellations": list(simulation.cancellations),
            "initial_cash": str(simulation.initial_cash),
            "initial_holdings": simulation.initial_holdings,
            "final_cash": str(simulation.final_cash),
            "final_holdings": simulation.final_holdings,
            "strategy_final_value": str(simulation.strategy_final_value),
            "baseline_final_value": str(simulation.baseline_final_value),
            "strategy_return": str(simulation.strategy_return),
            "baseline_return": str(simulation.baseline_return),
            "simulator_assumptions": simulation.assumptions,
            "execution_snapshot_id": simulation.snapshot_id,
        }
    base_value = Decimal(groups["kdj15_rsi0"]["strategy_final_value"])
    score_value = Decimal(groups["kdj8_rsi7"]["strategy_final_value"])
    momentum_value = Decimal(groups["kdj8_rsi7_rsi_momentum"]["strategy_final_value"])
    groups["kdj8_rsi7"]["incremental_value_vs_kdj15_rsi0"] = str(
        score_value - base_value
    )
    groups["kdj8_rsi7_rsi_momentum"]["incremental_value_vs_kdj8_rsi7"] = str(
        momentum_value - score_value
    )
    input_digest = _bars_digest(relevant)
    return {
        "issue": 83,
        "status": "exploratory_historical_account_simulation_not_certification",
        "strategy_id": STRATEGY_ID,
        "technical_parameter_hash": TECHNICAL_PARAMETER_HASH,
        "rule_source_sha256": RULE_SOURCE_SHA256,
        "group_analysis_parameter_hashes": {
            name: result["analysis_parameter_hash"] for name, result in groups.items()
        },
        "symbol": SYMBOL,
        "input": {
            "content_sha256": input_digest,
            "warmup_start": WARMUP_START.isoformat(),
            "warmup_bar_count": len(warmup_bars),
            "first_decision_bar": START.isoformat(),
            "last_decision_bar": decision_bars[-1].timestamp.isoformat(),
            "decision_bar_count": len(decision_bars),
            "relevant_bar_count_including_warmup": len(relevant),
        },
        "initial_account": {
            "as_of": INITIAL_ACCOUNT_TIME.isoformat(),
            "cash": "500000",
            "holdings": {},
            "open_orders": 0,
            "frozen_cash": "0",
            "liabilities": "0",
        },
        "execution": {
            "fill": "next available bar open",
            "slippage_bps": "0",
            "commission_bps": "0.5",
            "minimum_commission_yuan": "0.1",
            "board_lot_shares": 100,
            "t_plus_one": True,
            "participation_rate": "0.02",
            "zero_volume_fill": False,
            "order_lifetime_bars": 1,
        },
        "quantity_policy": {
            "type": "fixed_quantity_per_signal",
            "shares": 100,
            "limitation": (
                "Exploratory order sizing only; not V1 account-state sizing or "
                "protected-floor policy."
            ),
        },
        "groups": groups,
        "certification_gates_remaining": [
            (
                "This is an exploratory historical replay, not certified strategy "
                "evidence."
            ),
            "The fixed one-lot signal quantity is not the V1 account-state sizing, "
            "position cap, or protected-floor policy.",
            "Historical fill assumptions do not establish broker/exchange execution "
            "quality, live order-state reconciliation, or production readiness.",
            "The supplied market-history file's source completeness and "
            "corporate-action/data-quality certification are not established by this "
            "runner.",
        ],
        "reproduction": {
            "command": (
                "uv run python -m a500_t_trader intraday-compare-563360 --bars "
                "data/market-history/563360_5m_20240601_20261028.json --output "
                "issue83-comparison.json"
            ),
            "account_and_execution": (
                "All fixed parameters are embedded in run_issue83_comparison; all "
                "groups receive the same relevant bars, account snapshot, and "
                "simulator parameters."
            ),
        },
    }


def _bars_digest(bars: tuple[MinuteBar, ...]) -> str:
    document = [
        {
            "symbol": bar.symbol,
            "timestamp": bar.timestamp.isoformat(),
            "interval_minutes": bar.interval_minutes,
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": bar.volume,
            "amount": None if bar.amount is None else str(bar.amount),
            "is_completed": bar.is_completed,
        }
        for bar in bars
    ]
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _score_weight_hash(weights: dict[str, int]) -> str:
    canonical_weights = json.dumps(
        dict(sorted(weights.items())), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(
        f"{TECHNICAL_PARAMETER_HASH}:{canonical_weights}".encode()
    ).hexdigest()

def _run_issue85_sequential_replay(
    bars: tuple[MinuteBar, ...],
    *,
    score_thresholds: dict[str, int] | None = None,
    cost_multiplier: Decimal = Decimal(1),
    initial_cash: Decimal = Decimal("500000"),
) -> dict[str, object]:
    """Run full V1 replay with optional pre-registered stress parameters."""
    from .intraday import run_intraday_backtest

    ordered = tuple(
        sorted(
            (bar for bar in bars if bar.is_completed),
            key=lambda bar: bar.timestamp,
        )
    )
    pre_start = tuple(bar for bar in ordered if bar.timestamp < START)
    warmup = tuple(bar for bar in pre_start if bar.timestamp >= WARMUP_START)[-180:]
    decisions = tuple(bar for bar in ordered if START <= bar.timestamp <= END)
    if len(warmup) != 180 or len(decisions) != 23580 or decisions[0].timestamp != START:
        raise ValueError(
            f"expected 180 warmup and 23,580 decision bars; got "
            f"{len(warmup)} and {len(decisions)}"
        )
    account = AccountSnapshot(
        as_of=INITIAL_ACCOUNT_TIME,
        holdings={},
        cost_basis={},
        sellable={},
        available_cash=initial_cash,
    )
    execution_params = IntradayBacktestParams(
        participation_rate=Decimal("0.02"),
        fee_bps=Decimal("0.5") * cost_multiplier,
        slippage_bps=Decimal(0) * cost_multiplier,
        order_lifetime_bars=1,
        minimum_commission=Decimal("0.1") * cost_multiplier,
        dynamic_sizing=True,
    )
    signals = _SequentialV1Signals(
        warmup,
        pre_start,
        threshold_overrides=score_thresholds,
        fee_rate=execution_params.fee_bps / Decimal(10000),
        minimum_commission=execution_params.minimum_commission,
        impact_rate=Decimal("0.0002") * cost_multiplier,
    )
    result = run_intraday_backtest(
        symbol=SYMBOL,
        account=account,
        bars=decisions,
        signals=signals,
        params=execution_params,
        strategy_parameters={
            "source_strategy": STRATEGY_ID,
            "source_parameter_hash": TECHNICAL_PARAMETER_HASH,
            "rule_source_sha256": RULE_SOURCE_SHA256,
            "warmup_bars": 180,
            "sequential_decision_callback": True,
            "limitations": [
                "no source for independent intraday halt flags (historical provider status accepted per user)",
                "no independent spread/depth feed; 2% OHLCV participation is a proxy",
                "baseline slippage is zero; 1.5x cost replay scales fees but has no observed impact estimate",
                "one daily limit-field observation is invalid; no company-action or real-fill evidence",
            ],
        },
        implementation_version="issue85-sequential-replay-1",
    )
    pairs = result.pair_ledger
    closed = [pair for pair in pairs if pair["status"] == "closed"]

    def pair_metrics(selected: list[dict[str, object]]) -> dict[str, object]:
        pnl = [Decimal(str(pair["realized_net_pnl"])) for pair in selected]
        positive = sum((value for value in pnl if value > 0), Decimal(0))
        negative = -sum((value for value in pnl if value < 0), Decimal(0))
        return {
            "closed_pairs": len(selected),
            "paired_realized_net_pnl": str(sum(pnl, Decimal(0))),
            "average_net_pnl": str(sum(pnl, Decimal(0)) / len(pnl)) if pnl else None,
            "profit_factor": str(positive / negative) if negative else None,
        }

    trading_days = sorted({bar.timestamp.date() for bar in decisions})
    split_index = max(1, int(len(trading_days) * 0.70))
    split_day = trading_days[split_index]
    split = {
        "in_sample": pair_metrics(
            [pair for pair in closed if datetime.fromisoformat(str(pair["buyback_timestamp"])).date() < split_day]
        ),
        "out_of_sample": pair_metrics(
            [pair for pair in closed if datetime.fromisoformat(str(pair["buyback_timestamp"])).date() >= split_day]
        ),
        "split_day": split_day.isoformat(),
    }
    windows = []
    for index in range(5):
        start = int(len(trading_days) * index / 5)
        end = int(len(trading_days) * (index + 1) / 5)
        window_days = trading_days[start:end]
        if not window_days:
            continue
        lower, upper = window_days[0], window_days[-1]
        windows.append({
            "test_window": index + 1,
            "start_day": lower.isoformat(),
            "end_day": upper.isoformat(),
            **pair_metrics(
                [
                    pair
                    for pair in closed
                    if lower <= datetime.fromisoformat(str(pair["buyback_timestamp"])).date() <= upper
                ]
            ),
        })
    gross_profit = sum(
        (Decimal(str(pair["realized_net_pnl"])) for pair in closed if Decimal(str(pair["realized_net_pnl"])) > 0),
        Decimal(0),
    )
    gross_loss = -sum(
        (Decimal(str(pair["realized_net_pnl"])) for pair in closed if Decimal(str(pair["realized_net_pnl"])) < 0),
        Decimal(0),
    )
    return {
        "status": result.status,
        "research_scope": "candidate-driven sequential simulator; not complete V1 certification replay",
        "decision_bar_count": len(decisions),
        "first_decision_bar": decisions[0].timestamp.isoformat(),
        "last_decision_bar": decisions[-1].timestamp.isoformat(),
        "initial_cash": str(result.initial_cash),
        "initial_holdings": result.initial_holdings,
        "final_cash": str(result.final_cash),
        "final_holdings": result.final_holdings,
        "final_nav": str(result.strategy_final_value),
        "net_pnl": str(result.strategy_final_value - result.initial_cash),
        "return": str(result.strategy_return),
        "max_drawdown": str(_max_drawdown(result.account_states, initial_cash)),
        "signal_count": len(result.assumptions["submitted_signals"]),
        "fill_count": len(result.fills),
        "pair_count": len(pairs),
        **pair_metrics(closed),
        "winning_pair_count": sum(Decimal(str(pair["realized_net_pnl"])) > 0 for pair in closed),
        "losing_pair_count": sum(Decimal(str(pair["realized_net_pnl"])) < 0 for pair in closed),
        "split_70_30": split,
        "five_chronological_windows": windows,
        "walk_forward": {
            "assessed": False,
            "passed": False,
            "eligible_closed_pairs": len(closed),
            "reason": (
                "zero eligible closed pairs; expanding-window test PF cannot be estimated"
                if not closed
                else "expanding-window replay not implemented"
            ),
        },
        "open_pair_count": sum(pair["status"] == "open" for pair in pairs),
        "cancelled_order_count": len(result.cancellations),
        "limitations": result.strategy_parameters["limitations"],
        "cost_grid_examples": signals._cost_grid_examples,
    }

def run_issue85_sequential_replay(
    bars: tuple[MinuteBar, ...],
    *,
    run_stress_tests: bool = True,
    initial_cash: Decimal = Decimal("500000"),
) -> dict[str, object]:
    """Run baseline and, by default, full-data cost and threshold stresses."""
    baseline = _run_issue85_sequential_replay(bars, initial_cash=initial_cash)
    if not run_stress_tests:
        return baseline
    cost = _run_issue85_sequential_replay(
        bars, cost_multiplier=Decimal("1.5"), initial_cash=initial_cash
    )
    score_parameters = TECHNICAL_PARAMETERS["scores"]
    assert isinstance(score_parameters, dict)
    threshold_stresses: dict[str, object] = {}
    for name in ("buy", "sell", "take_profit", "buyback", "direction_delta"):
        original = int(score_parameters[name])
        for direction, factor in (("minus_10_percent", 0.9), ("plus_10_percent", 1.1)):
            value = int(original * factor + 0.5)
            stress = _run_issue85_sequential_replay(
                bars, score_thresholds={name: value}, initial_cash=initial_cash
            )
            threshold_stresses[f"{name}_{direction}"] = {
                "threshold": value,
                "net_pnl": stress["net_pnl"],
                "profit_factor": stress["profit_factor"],
                "closed_pairs": stress["closed_pairs"],
                "passed": Decimal(str(stress["net_pnl"])) > 0
                and stress["profit_factor"] is not None
                and Decimal(str(stress["profit_factor"])) > 1,
            }
    baseline["stress_tests"] = {
        "cost_1_5x": {
            "net_pnl": cost["net_pnl"],
            "profit_factor": cost["profit_factor"],
            "closed_pairs": cost["closed_pairs"],
            "fees_scaled": True,
            "slippage_bps_baseline": 0,
            "slippage_scaled": True,
            "passed": Decimal(str(cost["net_pnl"])) > 0
            and cost["profit_factor"] is not None
            and Decimal(str(cost["profit_factor"])) > 1,
        },
        "individual_score_thresholds_plus_minus_10_percent": threshold_stresses,
    }
    return baseline


def _max_drawdown(
    states: tuple[dict[str, object], ...], initial_cash: Decimal = Decimal("500000")
) -> Decimal:
    peak = initial_cash
    for state in states:
        nav = Decimal(str(state["nav_at_close"]))
        peak = max(peak, nav)
        if peak > 0:
            maximum = max(maximum, (peak - nav) / peak)
    return maximum
