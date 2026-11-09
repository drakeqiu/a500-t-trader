"""History and backtest command registration and execution."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from typing import cast

from ..backtest import BacktestMarketData, BacktestRepository, run_backtest
from ..market_data import (
    CapabilityUnavailableError,
    MarketDataService,
    normalize_symbol,
)
from ..strategy.daily_indicators import calculate_indicators
from ..strategy.premarket import load_account, load_config
from .common import DailyHistoryMarketData, MarketData, json_default
from .runtime import create_provider_from_env


def register_commands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    daily = commands.add_parser(
        "daily-history", help="query normalized daily OHLCV history"
    )
    daily.add_argument("--symbol", required=True)
    daily.add_argument("--start-date", required=True, type=date.fromisoformat)
    daily.add_argument("--end-date", required=True, type=date.fromisoformat)
    daily.add_argument(
        "--database",
        default=os.environ.get("DATABASE_URL"),
        required=not bool(os.environ.get("DATABASE_URL")),
        help="PostgreSQL connection URL",
    )
    daily.add_argument("--confirm-history-persistence-permitted", action="store_true")
    indicators = commands.add_parser(
        "daily-indicators",
        help="calculate close-based SMA(5), SMA(20), and Wilder RSI(14)",
    )
    indicators.add_argument("--symbol", required=True)
    indicators.add_argument("--start-date", required=True, type=date.fromisoformat)
    indicators.add_argument(
        "--database",
        default=os.environ.get("DATABASE_URL"),
        required=not bool(os.environ.get("DATABASE_URL")),
        help="PostgreSQL connection URL",
    )
    indicators.add_argument(
        "--confirm-history-persistence-permitted", action="store_true"
    )
    indicators.add_argument("--end-date", required=True, type=date.fromisoformat)
    backtest = commands.add_parser(
        "backtest", help="run an advisory minute-history backtest"
    )
    backtest.add_argument("--config", required=True, help="local ETF/rule JSON file")
    backtest.add_argument(
        "--account", required=True, help="initial user-entered account snapshot"
    )
    backtest.add_argument(
        "--symbol", help="selected configured ETF when multiple ETFs are configured"
    )
    backtest.add_argument("--start-date", required=True, type=date.fromisoformat)
    backtest.add_argument("--end-date", required=True, type=date.fromisoformat)
    backtest.add_argument("--interval-minutes", type=int, choices=(1, 5), default=1)
    backtest.add_argument(
        "--database",
        default=os.environ.get("DATABASE_URL"),
        required=not bool(os.environ.get("DATABASE_URL")),
        help="PostgreSQL connection URL",
    )
    backtest.add_argument("--cost-bps", type=Decimal, default=Decimal(0))
    backtest.add_argument("--slippage-bps", type=Decimal, default=Decimal(0))
    backtest.add_argument(
        "--minimum-commission",
        type=Decimal,
        default=Decimal(0),
        help="minimum commission charged independently for each fill; zero disables it",
    )
    backtest.add_argument("--implementation-version", default="1")
    backtest.add_argument(
        "--confirm-history-persistence-permitted",
        action="store_true",
        help="confirm the data provider permits local history/snapshot persistence",
    )
    cleanup = commands.add_parser(
        "history-cleanup", help="remove reusable shared daily and minute history"
    )
    cleanup.add_argument(
        "--database",
        default=os.environ.get("DATABASE_URL"),
        required=not bool(os.environ.get("DATABASE_URL")),
    )
    show = commands.add_parser(
        "backtest-show", help="show one run and its exact bar snapshot"
    )
    show.add_argument(
        "--database",
        default=os.environ.get("DATABASE_URL"),
        required=not bool(os.environ.get("DATABASE_URL")),
    )
    selected_run = show.add_mutually_exclusive_group(required=True)
    selected_run.add_argument("--latest", action="store_true")
    selected_run.add_argument("--run-id", type=int)


def handle(args: argparse.Namespace, market_data: MarketData | None) -> int:
    if args.command in ("daily-history", "daily-indicators"):
        try:
            if args.start_date > args.end_date:
                raise ValueError("start-date must be on or before end-date")
            if not args.confirm_history_persistence_permitted:
                print(
                    (
                        "Daily-history persistence is unauthorized; "
                        "confirm the provider terms before storing bars."
                    ),
                    file=sys.stderr,
                )
                return 2
            symbol = normalize_symbol(args.symbol)
            repository = BacktestRepository(args.database)
            bars = repository.get_shared_daily_history(
                symbol, args.start_date, args.end_date
            )
            if bars is None:
                source = market_data or MarketDataService(create_provider_from_env())
                bars = cast(DailyHistoryMarketData, source).get_daily_history(
                    symbol, args.start_date, args.end_date
                )
                repository.save_daily_history(
                    tuple(bars),
                    args.start_date,
                    args.end_date,
                    symbol=symbol,
                )
            records = (
                calculate_indicators(bars)
                if args.command == "daily-indicators"
                else bars
            )
        except Exception as error:
            print(
                f"Daily-history/indicator capability unavailable: {error}",
                file=sys.stderr,
            )
            return 2
        print(
            json.dumps(
                [asdict(row) for row in records],
                ensure_ascii=False,
                indent=2,
                default=json_default,
            )
        )
        return 0
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
            stored_backtest = BacktestRepository(args.database).get(
                None if args.latest else args.run_id
            )
        except Exception as error:
            print(f"Unable to read backtest history: {error}", file=sys.stderr)
            return 2
        if stored_backtest is None:
            print("No backtest is stored.", file=sys.stderr)
            return 2
        print(json.dumps(stored_backtest, ensure_ascii=False, indent=2))
        return 0
    try:
        config = load_config(args.config)
        account = load_account(args.account)
        if args.symbol is not None:
            symbol = normalize_symbol(args.symbol)
            matching = tuple(rule for rule in config.rules if rule.symbol == symbol)
            if not matching:
                raise ValueError(f"{symbol} is not present in configured ETF rules")
            config = replace(config, rules=matching)
        if len(config.rules) != 1:
            raise ValueError(
                "select --symbol when the configuration contains multiple ETFs"
            )
        if args.start_date > args.end_date:
            raise ValueError("start-date must be on or before end-date")
        if not args.confirm_history_persistence_permitted:
            print(
                (
                    "Minute-history persistence is not authorized; "
                    "confirm the provider terms before storing snapshots."
                ),
                file=sys.stderr,
            )
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
                    print(
                        f"Minute-history capability unavailable: {error}",
                        file=sys.stderr,
                    )
                    return 2
            minute_bars = cast(BacktestMarketData, market_data).get_history_bars(
                symbol, args.start_date, args.end_date, args.interval_minutes
            )
            if not minute_bars:
                print(
                    (
                        "Minute-history capability returned no bars "
                        "for the requested range."
                    ),
                    file=sys.stderr,
                )
                return 2
            selected_bars = tuple(
                sorted(
                    (
                        bar
                        for bar in minute_bars
                        if args.start_date <= bar.timestamp.date() <= args.end_date
                        and bar.is_completed
                    ),
                    key=lambda bar: bar.timestamp,
                )
            )
            if not selected_bars:
                print(
                    (
                        "Minute-history capability returned no completed bars "
                        "for the requested range."
                    ),
                    file=sys.stderr,
                )
                return 2
        backtest_result = run_backtest(
            config,
            account,
            selected_bars,
            start_date=args.start_date,
            end_date=args.end_date,
            interval_minutes=args.interval_minutes,
            cost_bps=args.cost_bps,
            slippage_bps=args.slippage_bps,
            minimum_commission=args.minimum_commission,
            implementation_version=args.implementation_version,
        )
        backtest_result = repository.save(selected_bars, backtest_result)
        print(
            json.dumps(
                asdict(backtest_result),
                ensure_ascii=False,
                indent=2,
                default=json_default,
            )
        )
        return 0
    except Exception as error:
        if isinstance(error, CapabilityUnavailableError):
            print(
                f"Required data capability unavailable or unauthorized: {error}",
                file=sys.stderr,
            )
        else:
            print(f"Unable to generate plan or backtest: {error}", file=sys.stderr)
        return 2
