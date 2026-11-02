"""Command-line entry point for advisory daily ETF workflows."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from . import account, history, intraday, planning
from .common import MarketData
from .parser import build_parser


def main(
    argv: Sequence[str] | None = None,
    *,
    market_data: MarketData | None = None,
    as_of: datetime | None = None,
) -> int:
    """Run CLI commands; injected data/time form the public CLI-to-app seam."""
    args = build_parser().parse_args(argv)
    if args.command in {"intraday-config", "intraday-strategy", "intraday-signals"}:
        return intraday.handle(args)
    if args.command in {
        "account-snapshot",
        "manual-fill",
        "account-ledger",
        "import-fills",
    }:
        return account.handle(args)
    if args.command in {
        "daily-history",
        "daily-indicators",
        "backtest",
        "history-cleanup",
        "backtest-show",
    }:
        return history.handle(args, market_data)
    return planning.handle(args, market_data, as_of)


if __name__ == "__main__":
    raise SystemExit(main())
