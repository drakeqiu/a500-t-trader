"""Reproducible exploratory Issue 83 source-rule comparison."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..market_data import SHANGHAI, MinuteBar
from ..strategy.intraday_technical import (
    TECHNICAL_PARAMETER_HASH,
    IntradayTechnicalAnalysis,
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


class _SequentialV1Signals:
    """Stateful V1 policy evaluated after each completed bar's prior fills."""

    def __init__(
        self,
        warmup: tuple[MinuteBar, ...],
        pre_start: tuple[MinuteBar, ...],
    ) -> None:
        self._history = list(warmup[-180:])
        self._grid_date = None
        self._used_levels: set[str] = set()
        self._current_step: Decimal | None = None
        self._lots: list[dict[str, object]] = []
        self._seen_fill_count = 0
        self._last_fill_by_side: dict[str, dict[str, object]] = {}
        self._daily_start_nav: Decimal | None = None
        self._day = pre_start[-1].timestamp.astimezone(SHANGHAI).date() if pre_start else None
        self._day_amount = Decimal(0)
        daily_amounts: dict[object, Decimal] = {}
        for prior in pre_start:
            if prior.amount is not None:
                day = prior.timestamp.astimezone(SHANGHAI).date()
                daily_amounts[day] = daily_amounts.get(day, Decimal(0)) + prior.amount
        if self._day is not None:
            self._day_amount = daily_amounts.pop(self._day, Decimal(0))
        self._completed_daily_amounts = [
            daily_amounts[day] for day in sorted(daily_amounts)
        ]
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

    @staticmethod
    def _round_trip_cost_rate(nav: Decimal, price: Decimal) -> Decimal:
        estimated_notional = min(nav * Decimal("0.08"), Decimal("100000"))
        fees_each_side = max(
            Decimal("0.1"), estimated_notional * Decimal("0.00005")
        )
        return fees_each_side * 2 / max(estimated_notional, price * Decimal(100))

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
        cost_rate = self._round_trip_cost_rate(nav, price)
        pending = bool(account_state["pending_orders"])
        paired_state: dict[str, object] = {}
        open_pairs = [
            pair
            for pair in account_state["pair_ledger"]
            if pair["status"] == "open" and int(pair["remaining_quantity"]) > 0
        ]
        if open_pairs:
            pair = open_pairs[0]
            sell_price = Decimal(str(pair["sell_price"]))
            frozen_step = Decimal(str(pair["sell_grid_step"] or "0"))
            paired_state["buyback"] = {
                "target_price": sell_price - max(
                    frozen_step, Decimal(3) * cost_rate * sell_price
                ),
                "quantity": int(pair["remaining_quantity"]),
                "trade_pair_id": pair["pair_id"],
            }
        active_lots = [lot for lot in self._lots if int(lot["quantity"]) > 0]
        if active_lots:
            lot = active_lots[0]
            entry = Decimal(str(lot["entry_price"]))
            grid = Decimal(str(lot["grid_step"]))
            paired_state["take_profit"] = {
                "target_price": entry + max(grid, Decimal(3) * cost_rate * entry),
                "quantity": int(lot["quantity"]),
            }
        grid_context = {
            "tick_size": tick,
            "round_trip_cost_rate": cost_rate,
            "grid_date": self._grid_date,
            "current_step": self._current_step,
            "used_levels": tuple(self._used_levels),
            "pending_grid_orders": pending,
        }
        analysis = analyze_intraday_technical(
            {symbol: history},
            grid_context={symbol: grid_context},
            paired_state={symbol: paired_state} if paired_state else {},
            as_of=bar.timestamp,
        )[symbol]
        analysis = _filter_rsi_momentum(analysis)
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
            quantity = min(
                int(pair["remaining_quantity"]),
                self._ordinary_quantity(nav, price, atr or Decimal(0), grid or Decimal(0)),
            )
            if quantity >= 100 and daily_return > Decimal("-0.008"):
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
            quantity = min(int(lot["quantity"]), sellable) // 100 * 100
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
            if int(account_state["daily_addition_count"]) >= 3:
                return ()
            quantity = self._ordinary_quantity(nav, price, atr, grid)
            if quantity < 100:
                return ()
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
            base_floor = int((nav * Decimal("0.30") / price) // 100) * 100
            reducible = max(0, holdings - base_floor)
            quantity = min(reducible, sellable, self._ordinary_quantity(nav, price, atr, grid))
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

def run_issue85_sequential_replay(bars: tuple[MinuteBar, ...]) -> dict[str, object]:
    """Run the V1 analyzer sequentially through the dynamic account simulator."""
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
        available_cash=Decimal("500000"),
    )
    result = run_intraday_backtest(
        symbol=SYMBOL,
        account=account,
        bars=decisions,
        signals=_SequentialV1Signals(warmup, pre_start),
        params=IntradayBacktestParams(
            participation_rate=Decimal("0.02"),
            fee_bps=Decimal("0.5"),
            slippage_bps=Decimal(0),
            order_lifetime_bars=1,
            minimum_commission=Decimal("0.1"),
            dynamic_sizing=True,
        ),
        strategy_parameters={
            "source_strategy": STRATEGY_ID,
            "source_parameter_hash": TECHNICAL_PARAMETER_HASH,
            "rule_source_sha256": RULE_SOURCE_SHA256,
            "warmup_bars": 180,
            "sequential_decision_callback": True,
            "limitations": [
                "no source for independent intraday halt flags (historical provider status accepted per user)",
                "no independent spread/depth feed; 2% OHLCV participation is a proxy",
                "the public API requires an upstream forced-risk-exit decision; no documented trigger is inferred",
                "take-profit is not emitted because the simulator ledger models sell-then-buyback pairs only",
                "grid-level use/cooling cannot be reconstructed from the current simulator state and is not claimed",
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
        "max_drawdown": str(_max_drawdown(result.account_states)),
        "signal_count": len(result.assumptions["submitted_signals"]),
        "fill_count": len(result.fills),
        "pair_count": len(pairs),
        **pair_metrics(closed),
        "winning_pair_count": sum(Decimal(str(pair["realized_net_pnl"])) > 0 for pair in closed),
        "losing_pair_count": sum(Decimal(str(pair["realized_net_pnl"])) < 0 for pair in closed),
        "split_70_30": split,
        "five_chronological_windows": windows,
        "profit_factor": str(gross_profit / gross_loss) if gross_loss else None,
        "open_pair_count": sum(pair["status"] == "open" for pair in pairs),
        "cancelled_order_count": len(result.cancellations),
        "limitations": result.strategy_parameters["limitations"],
    }


def _max_drawdown(states: tuple[dict[str, object], ...]) -> Decimal:
    peak = Decimal("500000")
    maximum = Decimal(0)
    for state in states:
        nav = Decimal(str(state["nav_at_close"]))
        peak = max(peak, nav)
        if peak > 0:
            maximum = max(maximum, (peak - nav) / peak)
    return maximum
