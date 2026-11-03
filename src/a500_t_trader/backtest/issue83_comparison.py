"""Reproducible exploratory Issue 83 source-rule comparison."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal

from ..market_data import SHANGHAI, MinuteBar
from ..strategy.intraday_technical import (
    RULE_SOURCE_SHA256,
    STRATEGY_ID,
    TECHNICAL_PARAMETER_HASH,
)
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


class _OneBoardLot:
    def quantity_for(self, symbol, analysis, bars):
        return 100


class _ExploratoryGridContext(TechnicalGridContextProvider):
    def context_for(self, symbol, bars):
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
    momentum_value = Decimal(
        groups["kdj8_rsi7_rsi_momentum"]["strategy_final_value"]
    )
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
