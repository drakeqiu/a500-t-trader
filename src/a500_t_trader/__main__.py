"""Command-line entry point for advisory daily ETF workflows."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import Sequence
from zoneinfo import ZoneInfo

from .market_data import CapabilityUnavailableError, MarketDataService
from .premarket import PlanMarketData, generate_plan, load_account, load_config, plan_json
from .runtime import create_provider_from_env


def main(
    argv: Sequence[str] | None = None,
    *,
    market_data: PlanMarketData | None = None,
    as_of: datetime | None = None,
) -> int:
    """Run CLI commands; injected data/time form the public CLI-to-app seam."""
    parser = argparse.ArgumentParser(prog="a500-t-trader")
    commands = parser.add_subparsers(dest="command", required=True)
    premarket = commands.add_parser("premarket", help="generate an advisory pre-market plan")
    premarket.add_argument("--config", required=True, help="local ETF/rule JSON file")
    premarket.add_argument("--account", required=True, help="user-entered account snapshot JSON")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        account = load_account(args.account)
        if market_data is None:
            market_data = MarketDataService(create_provider_from_env())
        now = as_of or datetime.now(ZoneInfo("Asia/Shanghai"))
        plan = generate_plan(config, account, market_data, as_of=now)
    except CapabilityUnavailableError as error:
        print(f"Required data capability unavailable or unauthorized: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"Unable to generate plan: {error}", file=sys.stderr)
        return 2
    print(plan_json(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
