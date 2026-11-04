"""Command-line entry point for advisory daily ETF workflows."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from . import account, history, intraday, planning
from .common import MarketData
from .live_signals import run_live_signals
from .parser import build_parser


def main(
    argv: Sequence[str] | None = None,
    *,
    market_data: MarketData | None = None,
    push_provider: object | None = None,
    as_of: datetime | None = None,
) -> int:
    """Run CLI commands; injected providers/time form public app seams."""
    args = build_parser().parse_args(argv)
    if args.command == "live-signals":
        if push_provider is None:
            import sys

            print(
                "live-signals requires an initialized TgwPushProvider "
                "supplied by the application",
                file=sys.stderr,
            )
            return 2
        return run_live_signals(
            push_provider,
            config_path=args.config,
            once=args.once,
            as_of=as_of,
        )
    if args.command in {
        "intraday-config",
        "intraday-strategy",
        "intraday-signals",
        "intraday-compare-563360",
        "intraday-replay-563360-v1",
    }:
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
