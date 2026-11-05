"""CLI for the independent signal-calculation consumer."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import timedelta

from .signal_calculator import run_signal_calculator


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="a500-signal-calculator")
    parser.add_argument("--purpose", default="signal-calculator")
    parser.add_argument("--max-age-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.max_age_seconds < 0:
        parser.error("--max-age-seconds must not be negative")
    run_signal_calculator(
        purpose=args.purpose,
        max_age=timedelta(seconds=args.max_age_seconds),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
