"""Command-line entry point for advisory daily ETF workflows."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from ..account_ledger import AccountLedger, import_galaxy_fills
from ..backtest import BacktestMarketData, BacktestRepository, run_backtest
from ..market_data import (
    CapabilityUnavailableError,
    DailyBar,
    MarketDataService,
    MinuteBar,
    normalize_symbol,
)
from ..strategy.daily_indicators import calculate_indicators
from ..strategy.intraday import MonitorMarketData, run_monitor
from ..strategy.intraday_api import evaluate_intraday_strategy
from ..strategy.intraday_v1_config import load_intraday_v1_config
from ..strategy.postmarket import (
    ReviewMarketData,
    generate_review,
    review_json,
    save_plan,
)
from ..strategy.premarket import (
    PlanMarketData,
    generate_plan,
    load_account,
    load_config,
    plan_json,
)
from .runtime import create_provider_from_env


class DailyHistoryMarketData(Protocol):
    def get_daily_history(
        self, symbol: str, start_date: date, end_date: date
    ) -> tuple[DailyBar, ...]: ...


def main(
    argv: Sequence[str] | None = None,
    *,
    market_data: PlanMarketData
    | BacktestMarketData
    | ReviewMarketData
    | MonitorMarketData
    | DailyHistoryMarketData
    | None = None,
    as_of: datetime | None = None,
) -> int:
    """Run CLI commands; injected data/time form the public CLI-to-app seam."""
    parser = argparse.ArgumentParser(prog="a500-t-trader")
    commands = parser.add_subparsers(dest="command", required=True)
    premarket = commands.add_parser(
        "premarket", help="generate an advisory pre-market plan"
    )
    premarket.add_argument("--config", required=True, help="local ETF/rule JSON file")
    premarket.add_argument(
        "--account", required=True, help="user-entered account snapshot JSON"
    )
    premarket.add_argument(
        "--output", help="save the plan and account snapshot as JSON"
    )
    premarket.add_argument("--database", help="SQLite account ledger")
    account_record = commands.add_parser(
        "account-snapshot", help="persist a user-entered account snapshot"
    )
    account_record.add_argument(
        "--account", required=True, help="user-entered account snapshot JSON"
    )
    account_record.add_argument(
        "--database", required=True, help="SQLite account ledger"
    )
    fill_record = commands.add_parser(
        "manual-fill", help="record a user-entered manual fill"
    )
    fill_record.add_argument("--timestamp", required=True, type=datetime.fromisoformat)
    fill_record.add_argument("--symbol", required=True)
    fill_record.add_argument("--side", required=True, choices=("buy", "sell"))
    fill_record.add_argument("--quantity", required=True, type=int)
    fill_record.add_argument("--price", required=True, type=Decimal)
    fill_record.add_argument("--notes", default="")
    fill_record.add_argument("--database", required=True, help="SQLite account ledger")
    fill_import = commands.add_parser(
        "import-fills", help="preview or import Galaxy Securities CSV fills"
    )
    fill_import.add_argument("--file", required=True, help="GB18030 Galaxy CSV file")
    fill_import.add_argument("--database", required=True, help="SQLite account ledger")
    fill_import.add_argument(
        "--confirm", action="store_true", help="persist valid new imported fills"
    )
    ledger_show = commands.add_parser(
        "account-ledger", help="show saved account snapshots and fills"
    )
    ledger_show.add_argument("--database", required=True, help="SQLite account ledger")
    daily = commands.add_parser(
        "daily-history", help="query normalized daily OHLCV history"
    )
    daily.add_argument("--symbol", required=True)
    daily.add_argument("--start-date", required=True, type=date.fromisoformat)
    daily.add_argument("--end-date", required=True, type=date.fromisoformat)
    daily.add_argument(
        "--database", required=True, help="local SQLite history database"
    )
    daily.add_argument("--confirm-history-persistence-permitted", action="store_true")
    indicators = commands.add_parser(
        "daily-indicators",
        help="calculate close-based SMA(5), SMA(20), and Wilder RSI(14)",
    )
    indicators.add_argument("--symbol", required=True)
    indicators.add_argument("--start-date", required=True, type=date.fromisoformat)
    indicators.add_argument(
        "--database", required=True, help="local SQLite history database"
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
        "--database", required=True, help="local SQLite history/backtest database"
    )
    backtest.add_argument("--cost-bps", type=Decimal, default=Decimal(0))
    backtest.add_argument("--slippage-bps", type=Decimal, default=Decimal(0))
    backtest.add_argument("--implementation-version", default="1")
    backtest.add_argument(
        "--confirm-history-persistence-permitted",
        action="store_true",
        help="confirm the data provider permits local history/snapshot persistence",
    )
    cleanup = commands.add_parser(
        "history-cleanup", help="remove reusable shared daily and minute history"
    )
    cleanup.add_argument("--database", required=True)
    show = commands.add_parser(
        "backtest-show", help="show one run and its exact bar snapshot"
    )
    show.add_argument("--database", required=True)
    selected_run = show.add_mutually_exclusive_group(required=True)
    selected_run.add_argument("--latest", action="store_true")
    selected_run.add_argument("--run-id", type=int)
    monitor = commands.add_parser(
        "monitor", help="start foreground intraday advisory monitoring"
    )
    monitor.add_argument("--config", required=True, help="local ETF/rule JSON file")
    monitor.add_argument(
        "--account", required=True, help="user-entered account snapshot JSON"
    )
    monitor.add_argument(
        "--database", required=True, help="SQLite trigger-evidence file"
    )
    monitor.add_argument(
        "--poll-seconds", type=float, default=5.0, help="foreground polling interval"
    )
    monitor.add_argument(
        "--once", action="store_true", help="evaluate one pass and exit"
    )
    review_parser = commands.add_parser(
        "postmarket", help="review a saved plan and daily evidence"
    )
    review_parser.add_argument(
        "--date", required=True, type=date.fromisoformat, help="reviewed trading date"
    )
    review_parser.add_argument(
        "--plan", required=True, help="saved pre-market plan JSON"
    )
    review_parser.add_argument(
        "--database", required=True, help="SQLite trigger-evidence file"
    )
    intraday_config = commands.add_parser(
        "intraday-config",
        help="validate explicit V1 strategy inputs and report their identity",
    )
    intraday_config.add_argument("--config", required=True, help="V1 JSON config file")
    strategy = commands.add_parser(
        "intraday-strategy", help="evaluate user-supplied intraday strategy inputs"
    )
    strategy.add_argument(
        "--config", required=True, help="explicit V1 strategy input JSON"
    )
    strategy.add_argument("--bars", required=True, help="minute bars JSON input")
    strategy.add_argument("--as-of", required=True, type=datetime.fromisoformat)
    strategy.add_argument("--output", help="export the exact structured result as JSON")
    args = parser.parse_args(argv)
    if args.command == "intraday-strategy":
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
                    None
                    if item.get("amount") is None
                    else Decimal(str(item["amount"])),
                    item.get("is_completed", True),
                )
                grouped.setdefault(bar.symbol, []).append(bar)
            result = evaluate_intraday_strategy(
                {symbol: tuple(bars) for symbol, bars in grouped.items()},
                as_of=args.as_of,
                config=config,
            )
            rendered = json.dumps(
                result, ensure_ascii=False, indent=2, default=_json_default
            )
            print(rendered)
            if args.output:
                with open(args.output, "w", encoding="utf-8") as stream:
                    stream.write(rendered + "\n")
            print(
                "Advisory only: this system does not connect to a broker; pending "
                "broker orders are unseen and may cause duplicate orders.",
                file=sys.stderr,
            )
            return 0
        except Exception as error:
            print(f"Unable to evaluate intraday strategy: {error}", file=sys.stderr)
            return 2
    if args.command == "intraday-config":
        try:
            config = load_intraday_v1_config(args.config)
        except Exception as error:
            print(f"Unable to load V1 intraday configuration: {error}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
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
    if args.command == "import-fills":
        try:
            report = import_galaxy_fills(args.database, args.file, confirm=args.confirm)
        except Exception:
            print(
                "Unable to import Galaxy fills: ledger operation failed.",
                file=sys.stderr,
            )
            return 2
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if report["errors"] else 0
    if args.command == "account-snapshot":
        try:
            identifier = AccountLedger(args.database).record_snapshot(
                load_account(args.account)
            )
        except Exception as error:
            print(f"Unable to record account snapshot: {error}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "id": identifier,
                    "classification": "user-entered; not broker-verified",
                }
            )
        )
        return 0
    if args.command == "manual-fill":
        try:
            identifier = AccountLedger(args.database).record_fill(
                args.timestamp,
                args.symbol,
                args.side,
                args.quantity,
                args.price,
                args.notes,
            )
        except Exception as error:
            print(f"Unable to record manual fill: {error}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "id": identifier,
                    "classification": "user-entered manual fill; not broker-verified",
                }
            )
        )
        return 0
    if args.command == "account-ledger":
        ledger = AccountLedger(args.database)
        print(
            json.dumps(
                {
                    "snapshots": ledger.snapshots(),
                    "manual_fills": ledger.fills(),
                    "imported_fills": ledger.imported_fills(),
                },
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            )
        )
        return 0
    if args.command in ("daily-history", "daily-indicators"):
        try:
            if args.start_date > args.end_date:
                raise ValueError("start-date must be on or before end-date")
            if not args.confirm_history_persistence_permitted:
                print(
                    "Daily-history persistence is unauthorized; "
                    "confirm the provider terms before storing bars.",
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
                query_source = cast(DailyHistoryMarketData, source)
                bars = query_source.get_daily_history(
                    symbol, args.start_date, args.end_date
                )
                if bars:
                    repository.save_daily_history(bars, args.start_date, args.end_date)
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
                default=_json_default,
            )
        )
        return 0
    if args.command == "postmarket":
        try:
            market_data_error = None
            if market_data is None:
                try:
                    market_data = MarketDataService(create_provider_from_env())
                except Exception as error:
                    market_data_error = str(error)
            review = generate_review(
                args.date,
                args.plan,
                args.database,
                cast(ReviewMarketData | None, market_data),
                market_data_error,
            )
            print(review_json(review))
            return 0
        except Exception as error:
            print(f"Unable to generate post-market review: {error}", file=sys.stderr)
            return 2

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
    if args.command == "monitor":
        try:
            config = load_config(args.config)
            account = load_account(args.account)
            if market_data is None:
                market_data = MarketDataService(create_provider_from_env())
            run_monitor(
                config,
                account,
                cast(MonitorMarketData, market_data),
                args.database,
                poll_seconds=args.poll_seconds,
                once=args.once,
                as_of=as_of,
            )
        except KeyboardInterrupt:
            print("Intraday monitoring stopped; no background process remains.")
        except CapabilityUnavailableError as error:
            print(
                f"Required monitoring capability unavailable or unauthorized: {error}",
                file=sys.stderr,
            )
            return 2
        except Exception as error:
            print(f"Unable to run intraday monitoring: {error}", file=sys.stderr)
            return 2
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
                raise ValueError(
                    "select --symbol when the configuration contains multiple ETFs"
                )
            if args.start_date > args.end_date:
                raise ValueError("start-date must be on or before end-date")
            if not args.confirm_history_persistence_permitted:
                print(
                    "Minute-history persistence is not authorized; "
                    "confirm the provider terms before storing snapshots.",
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
                        "Minute-history capability returned no bars "
                        "for the requested range.",
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
                        "Minute-history capability returned no completed "
                        "bars for the requested range.",
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
                implementation_version=args.implementation_version,
            )
            backtest_result = repository.save(selected_bars, backtest_result)
            print(
                json.dumps(
                    asdict(backtest_result),
                    ensure_ascii=False,
                    indent=2,
                    default=_json_default,
                )
            )
            return 0
        if market_data is None:
            market_data = MarketDataService(create_provider_from_env())
        now = as_of or datetime.now(ZoneInfo("Asia/Shanghai"))
        plan = generate_plan(
            config, account, cast(PlanMarketData, market_data), as_of=now
        )
    except CapabilityUnavailableError as error:
        print(
            f"Required data capability unavailable or unauthorized: {error}",
            file=sys.stderr,
        )
        return 2
    except Exception as error:
        print(f"Unable to generate plan or backtest: {error}", file=sys.stderr)
        return 2
    if args.output:
        try:
            save_plan(args.output, plan, account)
        except OSError as error:
            print(f"Unable to save pre-market plan: {error}", file=sys.stderr)
            return 2
    if args.database:
        try:
            snapshot_id = AccountLedger(args.database).record_snapshot(account)
            print(
                f"Account snapshot saved to SQLite ledger (id {snapshot_id}).",
                file=sys.stderr,
            )
        except Exception as error:
            print(f"Unable to persist account snapshot: {error}", file=sys.stderr)
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
