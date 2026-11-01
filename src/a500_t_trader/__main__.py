"""Command-line entry point for advisory daily ETF workflows."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Sequence
from zoneinfo import ZoneInfo

from .backtest import BacktestMarketData, BacktestRepository, run_backtest
from .market_data import CapabilityUnavailableError, MarketDataService, normalize_symbol
from .premarket import PlanMarketData, generate_plan, load_account, load_config, plan_json
from .runtime import create_provider_from_env


def main(
    argv: Sequence[str] | None = None,
    *,
    market_data: PlanMarketData | BacktestMarketData | None = None,
    as_of: datetime | None = None,
) -> int:
    """Run CLI commands; injected data/time form the public CLI-to-app seam."""
    parser = argparse.ArgumentParser(prog="a500-t-trader")
    commands = parser.add_subparsers(dest="command", required=True)
    premarket = commands.add_parser("premarket", help="generate an advisory pre-market plan")
    premarket.add_argument("--config", required=True, help="local ETF/rule JSON file")
    premarket.add_argument("--account", required=True, help="user-entered account snapshot JSON")
    backtest = commands.add_parser("backtest", help="backtest explicit rules on minute history")
    backtest.add_argument("--config", required=True, help="local ETF/rule JSON file")
    backtest.add_argument("--account", required=True, help="initial user-entered account snapshot")
    backtest.add_argument("--symbol", help="selected configured ETF when multiple ETFs are configured")
    backtest.add_argument("--start-date", required=True, type=date.fromisoformat)
    backtest.add_argument("--end-date", required=True, type=date.fromisoformat)
    backtest.add_argument("--interval-minutes", type=int, choices=(1, 5), default=1)
    backtest.add_argument("--database", required=True, help="local SQLite history/backtest database")
    backtest.add_argument("--cost-bps", type=Decimal, default=Decimal(0))
    backtest.add_argument("--slippage-bps", type=Decimal, default=Decimal(0))
    backtest.add_argument("--implementation-version", default="1")
    backtest.add_argument(
        "--confirm-history-persistence-permitted", action="store_true",
        help="confirm the data provider permits local history/snapshot persistence",
    )
    cleanup = commands.add_parser("history-cleanup", help="remove reusable shared minute history")
    cleanup.add_argument("--database", required=True)
    show = commands.add_parser("backtest-show", help="show one run and its exact bar snapshot")
    show.add_argument("--database", required=True)
    selected_run = show.add_mutually_exclusive_group(required=True)
    selected_run.add_argument("--latest", action="store_true")
    selected_run.add_argument("--run-id", type=int)
    args = parser.parse_args(argv)

    if args.command == "history-cleanup":
        try:
            deleted = BacktestRepository(args.database).cleanup_shared_history()
        except Exception as error:
            print(f"Unable to clean shared history: {error}", file=sys.stderr)
            return 2
        print(json.dumps({"deleted_shared_bars": deleted}))
        return 0
    if args.command == "backtest-show":
        try:
            result = BacktestRepository(args.database).get(
                None if args.latest else args.run_id
            )
        except Exception as error:
            print(f"Unable to read backtest history: {error}", file=sys.stderr)
            return 2
        if result is None:
            print("No backtest is stored.", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    try:
        config = load_config(args.config)
        account = load_account(args.account)
        if args.command == "backtest":
            if args.symbol is not None:
                symbol = normalize_symbol(args.symbol)
                matching = tuple(rule for rule in config.rules if rule.symbol == symbol)
                if not matching:
                    raise ValueError(f"{symbol} is not present in configured ETF rules")
                config = replace(config, rules=matching)
            if len(config.rules) != 1:
                raise ValueError("select --symbol when the configuration contains multiple ETFs")
            if args.start_date > args.end_date:
                raise ValueError("start-date must be on or before end-date")
            if not args.confirm_history_persistence_permitted:
                print("Minute-history persistence is not authorized; confirm the provider terms "
                      "before storing snapshots.", file=sys.stderr)
                return 2
            repository = BacktestRepository(args.database)
            symbol = config.rules[0].symbol
            selected_bars = repository.get_shared_history(
                symbol, args.start_date, args.end_date, args.interval_minutes
            )
            if selected_bars is None:
                if market_data is None:
                    try:
                        market_data = MarketDataService(create_provider_from_env())
                    except Exception as error:
                        print(f"Minute-history capability unavailable or unauthorized: {error}",
                              file=sys.stderr)
                        return 2
                bars = market_data.get_history_bars(
                    symbol, args.start_date, args.end_date, args.interval_minutes
                )
                if not bars:
                    print("Minute-history capability returned no bars for the requested range.",
                          file=sys.stderr)
                    return 2
                selected_bars = tuple(sorted(
                    (bar for bar in bars if args.start_date <= bar.timestamp.date() <= args.end_date
                     and bar.is_completed), key=lambda bar: bar.timestamp
                ))
                if not selected_bars:
                    print("Minute-history capability returned no completed bars for the requested range.",
                          file=sys.stderr)
                    return 2
            result = run_backtest(
                config, account, selected_bars, start_date=args.start_date,
                end_date=args.end_date, interval_minutes=args.interval_minutes,
                cost_bps=args.cost_bps, slippage_bps=args.slippage_bps,
                implementation_version=args.implementation_version,
            )
            result = repository.save(selected_bars, result)
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2,
                             default=_json_default))
            return 0
        if market_data is None:
            market_data = MarketDataService(create_provider_from_env())
        now = as_of or datetime.now(ZoneInfo("Asia/Shanghai"))
        plan = generate_plan(config, account, market_data, as_of=now)
    except CapabilityUnavailableError as error:
        print(f"Required data capability unavailable or unauthorized: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"Unable to generate plan or backtest: {error}", file=sys.stderr)
        return 2
    print(plan_json(plan))
    return 0


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
