"""Planning, review, and monitoring command registration and execution."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from typing import cast
from zoneinfo import ZoneInfo

from ..account_ledger import AccountLedger
from ..market_data import CapabilityUnavailableError, MarketDataService
from ..strategy.intraday import MonitorMarketData, run_monitor
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
from .common import MarketData
from .runtime import create_provider_from_env


def register_premarket(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
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
    premarket.add_argument(
        "--database",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL connection URL",
    )


def register_monitor_review(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    monitor = commands.add_parser(
        "monitor", help="start foreground intraday advisory monitoring"
    )
    monitor.add_argument("--config", required=True, help="local ETF/rule JSON file")
    monitor.add_argument(
        "--account", required=True, help="user-entered account snapshot JSON"
    )
    monitor.add_argument(
        "--database",
        default=os.environ.get("DATABASE_URL"),
        required=not bool(os.environ.get("DATABASE_URL")),
        help="PostgreSQL connection URL",
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
        "--database",
        default=os.environ.get("DATABASE_URL"),
        required=not bool(os.environ.get("DATABASE_URL")),
        help="PostgreSQL connection URL",
    )


def handle(
    args: argparse.Namespace, market_data: MarketData | None, as_of: datetime | None
) -> int:
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
        if market_data is None:
            market_data = MarketDataService(create_provider_from_env())
        plan = generate_plan(
            config,
            account,
            cast(PlanMarketData, market_data),
            as_of=as_of or datetime.now(ZoneInfo("Asia/Shanghai")),
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
                f"Account snapshot saved to PostgreSQL ledger (id {snapshot_id}).",
                file=sys.stderr,
            )
        except Exception as error:
            print(f"Unable to persist account snapshot: {error}", file=sys.stderr)
            return 2
    print(plan_json(plan))
    return 0
