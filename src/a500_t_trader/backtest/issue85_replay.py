"""Sequential Issue 85 V1 replay and its chronological validation runs."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from ..market_data import SHANGHAI, MinuteBar
from ..strategy.intraday_technical import TECHNICAL_PARAMETER_HASH, TECHNICAL_PARAMETERS
from ..strategy.intraday_v1_config import RULE_SOURCE_SHA256, STRATEGY_ID
from ..strategy.premarket import AccountSnapshot
from .intraday import (
    IntradayBacktestParams,
    run_intraday_backtest,
)
from .issue83_comparison import (
    END,
    INITIAL_ACCOUNT_TIME,
    START,
    SYMBOL,
)
from .session_amount import complete_session_amount


def _ohlcv_spread_proxy_eligible(
    prior_bar: MinuteBar | None,
    reference_price: Decimal,
    tick_size: Decimal,
) -> bool:
    """Use half the prior completed bar's range as a spread proxy, not a quote."""
    if (
        prior_bar is None
        or not prior_bar.is_completed
        or prior_bar.high < prior_bar.low
        or prior_bar.low <= 0
        or reference_price <= 0
        or tick_size <= 0
    ):
        return False
    proxy = (prior_bar.high - prior_bar.low) / (Decimal(2) * reference_price)
    return proxy <= max(Decimal("0.0015"), Decimal(2) * tick_size / reference_price)


ZERO_SLIPPAGE_ASSUMPTION = {
    "bps_per_side": "0",
    "source": "exploratory baseline assumption recorded in docs/backtest-563360-v1.md",
    "recorded_on": "2026-11-03",
    "context": (
        "idealized next-available-bar-open baseline; not observed fill or "
        "market-impact evidence"
    ),
    "observed": False,
}


def _max_affordable_quantity(
    free_cash: Decimal,
    price: Decimal,
    fee_rate: Decimal,
    impact_rate: Decimal,
    minimum_commission: Decimal,
) -> int:
    cash_per_share = price * (Decimal(1) + fee_rate + impact_rate) + (
        minimum_commission / Decimal(100)
    )
    return int((free_cash / cash_per_share) // 100) * 100


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
        self._liquidity_quantity_examples: list[dict[str, object]] = []
        self._seen_fill_count = 0
        self._last_fill_by_side: dict[str, dict[str, object]] = {}
        self._daily_start_nav: Decimal | None = None
        self._day = (
            pre_start[-1].timestamp.astimezone(SHANGHAI).date() if pre_start else None
        )
        self._day_amount = Decimal(0)
        daily_bars: dict[object, list[MinuteBar]] = {}
        for prior in pre_start:
            day = prior.timestamp.astimezone(SHANGHAI).date()
            daily_bars.setdefault(day, []).append(prior)
        self._completed_daily_amounts = []
        for day, session in sorted(daily_bars.items()):
            if day == self._day:
                continue
            amount = complete_session_amount(session)
            if amount is not None:
                self._completed_daily_amounts.append(amount)
        if self._day is not None:
            self._day_amount = sum(
                (bar.amount for bar in daily_bars[self._day] if bar.amount is not None),
                Decimal(0),
            )
        self._last_close: Decimal | None = None
        self._opposite_anchor_closes = 0
        self._anchor_side: int | None = None
        self._entry_pause_until = -1
        self._last_nav: Decimal | None = None
        self._daily_reset_examples: list[dict[str, str]] = []

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

    def _liquidity_quantity(self, bar: MinuteBar) -> int:
        """Cap one decision to 2% of its last completed bar's volume."""
        return (bar.volume * 2 // 100) // 100 * 100

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
            self._daily_start_nav = (
                self._last_nav if self._last_nav is not None else nav
            )
            self._daily_reset_examples.append(
                {
                    "date": session_date.isoformat(),
                    "previous_close_nav": (
                        str(self._last_nav) if self._last_nav is not None else str(nav)
                    ),
                    "first_decision_nav": str(nav),
                    "daily_start_nav": str(self._daily_start_nav),
                    "first_decision_daily_return": str(
                        (nav - self._daily_start_nav) / self._daily_start_nav
                        if self._daily_start_nav
                        else Decimal(0)
                    ),
                }
            )
            self._used_levels.clear()
            self._current_step = None
            self._opposite_anchor_closes = 0
            self._anchor_side = None
        elif self._daily_start_nav is None:
            self._daily_start_nav = nav
        self._last_nav = nav
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
                    quantity = min(
                        quantity,
                        _max_affordable_quantity(
                            Decimal(str(account_state["free_cash"])),
                            price,
                            self._fee_rate,
                            self._impact_rate,
                            self._minimum_commission,
                        ),
                    )
                    if (
                        int(account_state["daily_addition_count"]) >= 3
                        or int(account_state["remaining_daily_additions"]) <= 0
                    ):
                        quantity = 0
                elif candidate == "BUYBACK":
                    quantity = min(
                        quantity,
                        _max_affordable_quantity(
                            Decimal(str(account_state["free_cash"])),
                            price,
                            self._fee_rate,
                            self._impact_rate,
                            self._minimum_commission,
                        ),
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
            quantity = min(quantity, self._liquidity_quantity(bar))
            if quantity >= 100 and len(self._liquidity_quantity_examples) < 100:
                self._liquidity_quantity_examples.append(
                    {
                        "quantity": quantity,
                        "decision_bar_volume": bar.volume,
                    }
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
            quantity = min(
                min(sellable, holdings, risk_exit_quantity or sellable),
                self._liquidity_quantity(bar),
            )
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
        if (
            daily_return <= Decimal("-0.008")
            or current_index <= self._entry_pause_until
        ):
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
            max_affordable = _max_affordable_quantity(
                Decimal(str(account_state["free_cash"])),
                price,
                self._fee_rate,
                self._impact_rate,
                self._minimum_commission,
            )
            quantity = min(
                pair_limit,
                self._ordinary_quantity(
                    nav, price, atr or Decimal(0), grid or Decimal(0)
                ),
                max_affordable,
                int(Decimal(str(account_state["remaining_daily_turnover"])) / price)
                // 100
                * 100,
            )
            quantity = min(quantity, self._liquidity_quantity(bar))
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
                        spread_eligible=_ohlcv_spread_proxy_eligible(
                            self._history[-2] if len(self._history) > 1 else None,
                            price,
                            Decimal("0.001"),
                        ),
                    ),
                )
            return ()
        if candidate == "TAKE_PROFIT" and active_lots and sellable:
            lot = active_lots[0]
            quantity = (
                min(
                    int(lot["quantity"]),
                    sellable,
                    int(
                        Decimal(str(account_state["remaining_daily_sell_notional"]))
                        / price
                    ),
                    self._liquidity_quantity(bar),
                )
                // 100
                * 100
            )
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
            quantity = min(
                self._ordinary_quantity(nav, price, atr, grid),
                _max_affordable_quantity(
                    Decimal(str(account_state["free_cash"])),
                    price,
                    self._fee_rate,
                    self._impact_rate,
                    self._minimum_commission,
                ),
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
                    spread_eligible=_ohlcv_spread_proxy_eligible(
                        self._history[-2] if len(self._history) > 1 else None,
                        price,
                        Decimal("0.001"),
                    ),
                ),
            )
        elif candidate == "SELL":
            if int(account_state["remaining_daily_fills"]) <= 0:
                return ()
            base_floor = int((nav * Decimal("0.30") / price) // 100) * 100
            reducible = max(0, holdings - base_floor)
            quantity = min(
                reducible,
                sellable,
                self._ordinary_quantity(nav, price, atr, grid),
                int(
                    Decimal(str(account_state["remaining_daily_sell_notional"])) / price
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


def _walk_forward_gate(run: dict[str, object]) -> dict[str, object]:
    profit_factor = run["profit_factor"]
    enough_samples = int(run["closed_pairs"]) > 0 and profit_factor is not None
    if not enough_samples:
        return {
            "status": "undefined",
            "assessed": False,
            "passed": False,
            "closed_pairs": run["closed_pairs"],
            "net_pnl": run["paired_realized_net_pnl"],
            "profit_factor": profit_factor,
        }
    passed = (
        Decimal(str(run["paired_realized_net_pnl"])) > 0
        and Decimal(str(profit_factor)) > 1
    )
    return {
        "status": "passed" if passed else "failed",
        "assessed": True,
        "passed": passed,
        "closed_pairs": run["closed_pairs"],
        "net_pnl": run["paired_realized_net_pnl"],
        "profit_factor": profit_factor,
    }


def _run_walk_forward(
    bars: tuple[MinuteBar, ...],
    trading_days: list[date],
    *,
    initial_cash: Decimal,
) -> dict[str, object]:
    boundaries = [int(len(trading_days) * index / 10) for index in range(11)]
    windows: list[dict[str, object]] = []
    for index in range(5):
        train_days = trading_days[: boundaries[5 + index]]
        test_days = trading_days[boundaries[5 + index] : boundaries[6 + index]]
        if not train_days or not test_days:
            continue
        train_start = datetime.combine(trading_days[0], time.min, tzinfo=SHANGHAI)
        train_end = datetime.combine(train_days[-1], time.max, tzinfo=SHANGHAI)
        test_start = datetime.combine(test_days[0], time.min, tzinfo=SHANGHAI)
        test_end = datetime.combine(test_days[-1], time.max, tzinfo=SHANGHAI)
        train_run = _run_issue85_sequential_replay(
            bars,
            initial_cash=initial_cash,
            decision_start=max(START, train_start),
            decision_end=train_end,
            include_walk_forward=False,
        )
        test_run = _run_issue85_sequential_replay(
            bars,
            initial_cash=initial_cash,
            decision_start=test_start,
            decision_end=test_end,
            include_walk_forward=False,
        )
        windows.append(
            {
                "window": index + 1,
                "training_start_day": train_days[0].isoformat(),
                "training_end_day": train_days[-1].isoformat(),
                "test_start_day": test_days[0].isoformat(),
                "test_end_day": test_days[-1].isoformat(),
                "training_run": {
                    "decision_bar_count": train_run["decision_bar_count"],
                    "closed_pairs": train_run["closed_pairs"],
                    "paired_realized_net_pnl": train_run["paired_realized_net_pnl"],
                    "profit_factor": train_run["profit_factor"],
                },
                "training_gate": _walk_forward_gate(train_run),
                "test_run": {
                    "decision_bar_count": test_run["decision_bar_count"],
                    "closed_pairs": test_run["closed_pairs"],
                    "paired_realized_net_pnl": test_run["paired_realized_net_pnl"],
                    "profit_factor": test_run["profit_factor"],
                },
                "test_gate": _walk_forward_gate(test_run),
            }
        )
    test_gates = [window["test_gate"] for window in windows]
    assessed = sum(gate["assessed"] for gate in test_gates)
    passed = assessed >= 4 and sum(gate["passed"] for gate in test_gates) >= 4
    return {
        "method": "five expanding training prefixes with contiguous later test blocks",
        "assessed": assessed == len(windows) and len(windows) == 5,
        "passed": passed,
        "windows": windows,
    }


def _run_chronological_split(
    bars: tuple[MinuteBar, ...],
    trading_days: list[date],
    *,
    initial_cash: Decimal,
) -> dict[str, object]:
    split_index = int(len(trading_days) * 0.7)
    training_days = trading_days[:split_index]
    test_days = trading_days[split_index:]
    if not training_days or not test_days:
        raise ValueError("70/30 chronological split requires at least two days")
    training_run = _run_issue85_sequential_replay(
        bars,
        initial_cash=initial_cash,
        decision_start=max(
            START,
            datetime.combine(training_days[0], time.min, tzinfo=SHANGHAI),
        ),
        decision_end=datetime.combine(training_days[-1], time.max, tzinfo=SHANGHAI),
        include_walk_forward=False,
    )
    test_run = _run_issue85_sequential_replay(
        bars,
        initial_cash=initial_cash,
        decision_start=datetime.combine(test_days[0], time.min, tzinfo=SHANGHAI),
        decision_end=datetime.combine(test_days[-1], time.max, tzinfo=SHANGHAI),
        include_walk_forward=False,
    )

    def summary(run: dict[str, object]) -> dict[str, object]:
        return {
            "decision_bar_count": run["decision_bar_count"],
            "closed_pairs": run["closed_pairs"],
            "paired_realized_net_pnl": run["paired_realized_net_pnl"],
            "profit_factor": run["profit_factor"],
        }

    return {
        "method": "chronological 70/30 trading-day split",
        "training_start_day": training_days[0].isoformat(),
        "training_end_day": training_days[-1].isoformat(),
        "test_start_day": test_days[0].isoformat(),
        "test_end_day": test_days[-1].isoformat(),
        "training_day_count": len(training_days),
        "test_day_count": len(test_days),
        "training_run": summary(training_run),
        "training_gate": _walk_forward_gate(training_run),
        "test_run": summary(test_run),
        "test_gate": _walk_forward_gate(test_run),
    }


def _run_issue85_sequential_replay(
    bars: tuple[MinuteBar, ...],
    score_thresholds: dict[str, int] | None = None,
    cost_multiplier: Decimal = Decimal(1),
    initial_cash: Decimal = Decimal("500000"),
    decision_start: datetime = START,
    decision_end: datetime = END,
    include_walk_forward: bool = True,
) -> dict[str, object]:
    """Replay one contiguous period, with the preceding 180 bars as warmup."""
    ordered = tuple(
        sorted(
            (bar for bar in bars if bar.is_completed),
            key=lambda bar: bar.timestamp,
        )
    )
    pre_start = tuple(bar for bar in ordered if bar.timestamp < decision_start)
    warmup = pre_start[-180:]
    decisions = tuple(
        bar for bar in ordered if decision_start <= bar.timestamp <= decision_end
    )
    if len(warmup) != 180 or not decisions:
        raise ValueError(
            f"expected 180 warmup and nonempty decision period; got "
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
            "zero_slippage_assumption": ZERO_SLIPPAGE_ASSUMPTION,
            "limitations": [
                "no source for independent intraday halt flags "
                "(historical provider status accepted per user)",
                "baseline slippage is a recorded zero-bps assumption; 1.5x cost "
                "replay scales fees, minimum commission, modeled impact, and "
                "slippage input (which remains zero), without observed impact "
                "evidence",
                "modeled 0.02% per-side impact is used for policy cost estimates "
                "and is not applied a second time to simulated fill prices",
                "AmazingData's 2026-04-20 limit fields are invalid; its theoretical "
                "1.188/1.452 band is reconstructed from the 2026-04-17 SSE close "
                "and published 10%/0.001 rules, not a direct historical limit "
                "field",
                "complete corporate-action ledger and broker-authenticated "
                "realized fills/impact remain unavailable and were waived by the "
                "user as certification prerequisites",
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
    walk_forward = (
        _run_walk_forward(bars, trading_days, initial_cash=initial_cash)
        if include_walk_forward
        else None
    )
    chronological_split = (
        _run_chronological_split(bars, trading_days, initial_cash=initial_cash)
        if include_walk_forward
        else None
    )
    submitted_signals = result.assumptions["submitted_signals"]
    limitations = result.strategy_parameters["limitations"]
    return {
        "status": result.status,
        "research_scope": (
            "candidate-driven sequential simulator; not complete V1 "
            "certification replay"
        ),
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
        "signal_count": len(submitted_signals),
        "submitted_signals": list(submitted_signals),
        "fill_count": len(result.fills),
        "pair_count": len(pairs),
        **pair_metrics(closed),
        "winning_pair_count": sum(
            Decimal(str(pair["realized_net_pnl"])) > 0 for pair in closed
        ),
        "losing_pair_count": sum(
            Decimal(str(pair["realized_net_pnl"])) < 0 for pair in closed
        ),
        "chronological_split": chronological_split,
        "walk_forward": walk_forward,
        "inputs": {
            "fee_bps": str(execution_params.fee_bps),
            "minimum_commission": str(execution_params.minimum_commission),
            "slippage_bps": str(execution_params.slippage_bps),
            "impact_bps": str(Decimal("0.0002") * cost_multiplier * Decimal(10000)),
            "cost_multiplier": str(cost_multiplier),
        },
        "daily_reset_examples": signals._daily_reset_examples,
        "account_states": list(result.account_states),
        "assumptions": {
            "fee": {
                "value": str(execution_params.fee_bps),
                "unit": "bps per side",
                "source": "user-provided commission rate, recorded in "
                "docs/backtest-563360-v1.md",
                "context": "applied independently to each simulated fill",
                "observed": False,
            },
            "minimum_commission": {
                "value": str(execution_params.minimum_commission),
                "unit": "CNY per fill",
                "source": "user-provided minimum commission, recorded in "
                "docs/backtest-563360-v1.md",
                "context": "applied independently to each simulated fill",
                "observed": False,
            },
            "slippage": {
                "value": str(execution_params.slippage_bps),
                "unit": "bps per side",
                "source": "exploratory baseline assumption recorded in "
                "docs/backtest-563360-v1.md",
                "context": "idealized next-available-bar-open baseline; not "
                "observed fill evidence",
                "observed": False,
            },
            "impact": {
                "value": str(Decimal("0.0002") * cost_multiplier * Decimal(10000)),
                "unit": "bps per side",
                "source": "modeled cost-grid assumption in the Issue 85 replay",
                "context": "used to estimate candidate cost only; not applied "
                "again to simulated fill prices",
                "observed": False,
            },
            "cost_multiplier": {
                "value": str(cost_multiplier),
                "unit": "x",
                "source": "registered baseline or stress replay configuration",
                "context": "multiplies modeled fees, minimum commission, "
                "slippage, and impact inputs",
            },
            "zero_slippage_assumption": ZERO_SLIPPAGE_ASSUMPTION,
            "spread_proxy": {
                "formula": "0.5 * (previous completed bar high - low) / decision price",
                "eligibility_limit": "max(0.0015, 2 * tick_size / decision price)",
                "tick_size": "0.001",
                "source": "user-approved OHLCV modeling substitute",
                "context": "proxy only; not observed historical bid/ask spread "
                "or execution evidence",
                "observed": False,
            },
        },
        "liquidity_quantity_examples": signals._liquidity_quantity_examples,
        "open_pair_count": sum(pair["status"] == "open" for pair in pairs),
        "cancelled_order_count": len(result.cancellations),
        "limitations": limitations,
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
        bars,
        cost_multiplier=Decimal("1.5"),
        initial_cash=initial_cash,
        include_walk_forward=False,
    )
    score_parameters = TECHNICAL_PARAMETERS["scores"]
    assert isinstance(score_parameters, dict)
    threshold_stresses: dict[str, object] = {}
    for name in ("buy", "sell", "take_profit", "buyback", "direction_delta"):
        original = int(score_parameters[name])
        for direction, factor in (("minus_10_percent", 0.9), ("plus_10_percent", 1.1)):
            value = int(original * factor + 0.5)
            stress = _run_issue85_sequential_replay(
                bars,
                score_thresholds={name: value},
                initial_cash=initial_cash,
                include_walk_forward=False,
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
            "cost_multiplier": cost["inputs"]["cost_multiplier"],
            "inputs": {
                "fee_bps": cost["inputs"]["fee_bps"],
                "minimum_commission": cost["inputs"]["minimum_commission"],
                "slippage_bps": cost["inputs"]["slippage_bps"],
                "impact_bps": cost["inputs"]["impact_bps"],
            },
            "fees_scaled": True,
            "minimum_commission_scaled": True,
            "slippage_scaled": True,
            "impact_scaled": True,
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
    maximum = Decimal(0)
    for state in states:
        nav = Decimal(str(state["nav_at_close"]))
        peak = max(peak, nav)
        if peak > 0:
            maximum = max(maximum, (peak - nav) / peak)
    return maximum
