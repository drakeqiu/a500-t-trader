"""Intraday strategy command registration and execution."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

from ..backtest.issue83_comparison import run_issue83_comparison
from ..backtest.intraday_signals import replay_fixed_quantity_signals
from ..market_data import MinuteBar
from ..strategy.intraday_api import evaluate_intraday_strategy
from ..strategy.intraday_v1_config import load_intraday_v1_config
from .common import json_default


def _load_completed_bars(path: str) -> tuple[MinuteBar, ...]:
    with open(path, encoding="utf-8") as stream:
        document = json.load(stream)
    if not isinstance(document, dict) or not isinstance(document.get("bars"), list):
        raise ValueError("bars input must be a JSON object containing bars")
    return tuple(
        MinuteBar(
            item["symbol"],
            datetime.fromisoformat(item["timestamp"]),
            item["interval_minutes"],
            Decimal(str(item["open"])),
            Decimal(str(item["high"])),
            Decimal(str(item["low"])),
            Decimal(str(item["close"])),
            item["volume"],
            None if item.get("amount") is None else Decimal(str(item["amount"])),
            item.get("is_completed", True),
        )
        for item in document["bars"]
    )



def register_commands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    config_parser = commands.add_parser(
        "intraday-config",
        help="validate explicit V1 strategy inputs and report their identity",
    )
    config_parser.add_argument("--config", required=True, help="V1 JSON config file")
    strategy = commands.add_parser(
        "intraday-strategy", help="evaluate user-supplied intraday strategy inputs"
    )
    strategy.add_argument(
        "--config", required=True, help="explicit V1 strategy input JSON"
    )
    strategy.add_argument("--bars", required=True, help="minute bars JSON input")
    strategy.add_argument("--as-of", required=True, type=datetime.fromisoformat)
    strategy.add_argument("--output", help="export the exact structured result as JSON")
    signal_replay = commands.add_parser(
        "intraday-signals",
        help="replay V1 BUY/SELL candidates with a fixed signal quantity",
    )
    signal_replay.add_argument(
        "--bars",
        default="data/market-history/563360_5m_20240601_20261028.json",
        help="saved five-minute history JSON",
    )
    signal_replay.add_argument(
        "--quantity", type=int, default=40000, help="fixed signal quantity in shares"
    )
    signal_replay.add_argument("--output", help="optional JSON output file")
    comparison = commands.add_parser(
        "intraday-compare-563360",
        help="run the exploratory Issue 83 three-group account comparison",
    )
    comparison.add_argument(
        "--bars",
        default="data/market-history/563360_5m_20240601_20261028.json",
        help="saved five-minute history JSON",
    )
    comparison.add_argument("--output", help="optional JSON output file")


def handle(args: argparse.Namespace) -> int:
    if args.command == "intraday-config":
        try:
            config = load_intraday_v1_config(args.config)
        except Exception as error:
            print(f"Unable to load V1 intraday configuration: {error}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "schema_version": config.schema_version,
                    "strategy_id": config.strategy_id,
                    "rule_version": config.rule_version,
                    "implementation_version": config.implementation_version,
                    "rule_source": config.rule_source,
                    "rule_source_sha256": config.rule_source_sha256,
                    "configuration_fingerprint": config.fingerprint,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "intraday-compare-563360":
        try:
            minute_bars = _load_completed_bars(args.bars)
            return _print_result(run_issue83_comparison(minute_bars), args.output)
        except Exception as error:
            print(f"Unable to run Issue 83 comparison: {error}", file=sys.stderr)
            return 2
    if args.command == "intraday-signals":
        try:
            minute_bars = _load_completed_bars(args.bars)
            if not minute_bars:
                raise ValueError("bars input contains no bars")
            symbol = minute_bars[0].symbol
            if any(bar.symbol != symbol for bar in minute_bars):
                raise ValueError("signal replay accepts one symbol per input")
            signals = replay_fixed_quantity_signals(
                symbol, minute_bars, quantity=args.quantity
            )
            result = {
                "strategy": "intraday-etf-t-v1-technical-candidate-replay",
                "research_status": "historical_research_only",
                "rsi_comparison_eligible": False,
                "rsi_certification_eligible": False,
                "evidence_scope": (
                    "Fixed-share technical replay is historical research only; it is not "
                    "a complete account backtest and is excluded from RSI comparison "
                    "and certification evidence."
                ),
                "symbol": symbol,
                "signal_quantity": args.quantity,
                "first_bar": minute_bars[0].timestamp,
                "last_bar": minute_bars[-1].timestamp,
                "input_bar_count": len(minute_bars),
                "signal_count": len(signals),
                "signals": [asdict(signal) for signal in signals],
                "starting_account_snapshot": {
                    "cash": "210000",
                    "holding_quantity": 400700,
                    "cost_price": "1.193",
                    "protected_floor_quantity": 280500,
                    "sellable_quantity_at_start": 120200,
                    "fixed_quantity_sell_slots_at_start": 3,
                },
                "limitations": [
                    "no fills, P&L, position updates, T+1 or sell-capacity checks",
                    "starting account snapshot is informational, not a signal gate",
                    "grid tick uses the SSE ETF minimum increment of 0.001 yuan",
                    "0.5 bps commission, 0.1 yuan minimum; no slippage is modeled",
                    "missing history is not synthesized",
                    "grid-level cooldown is absent; repeated candidates may occur",
                    "fixed-share technical replay is research only and excluded from RSI "
                    "comparison and certification evidence",
                    "daily bar completeness has not been independently audited",
                ],
            }
            return _print_result(result, args.output)
        except Exception as error:
            print(
                f"Unable to replay fixed-quantity V1 signals: {error}", file=sys.stderr
            )
            return 2
    try:
        config = load_intraday_v1_config(args.config)
        with open(args.bars, encoding="utf-8") as stream:
            document = json.load(stream)
        if not isinstance(document, list):
            raise ValueError("bars input must be a JSON array")
        grouped: dict[str, list[MinuteBar]] = {}
        for item in document:
            if not isinstance(item, dict):
                raise ValueError("each bar must be a JSON object")
            bar = MinuteBar(
                item["symbol"],
                datetime.fromisoformat(item["timestamp"]),
                item["interval_minutes"],
                Decimal(str(item["open"])),
                Decimal(str(item["high"])),
                Decimal(str(item["low"])),
                Decimal(str(item["close"])),
                item["volume"],
                None if item.get("amount") is None else Decimal(str(item["amount"])),
                item.get("is_completed", True),
            )
            grouped.setdefault(bar.symbol, []).append(bar)
        result = evaluate_intraday_strategy(
            {symbol: tuple(bars) for symbol, bars in grouped.items()},
            as_of=args.as_of,
            config=config,
        )
        rendered = json.dumps(
            result, ensure_ascii=False, indent=2, default=json_default
        )
        print(rendered)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as stream:
                stream.write(rendered + "\n")
        print(
            (
                "Advisory only: this system does not connect to a broker; pending "
                "broker orders are unseen and may cause duplicate orders."
            ),
            file=sys.stderr,
        )
        return 0
    except Exception as error:
        print(f"Unable to evaluate intraday strategy: {error}", file=sys.stderr)
        return 2


def _print_result(result: object, output: str | None) -> int:
    rendered = json.dumps(result, ensure_ascii=False, indent=2, default=json_default)
    print(rendered)
    if output:
        with open(output, "w", encoding="utf-8") as stream:
            stream.write(rendered + "\n")
    return 0
