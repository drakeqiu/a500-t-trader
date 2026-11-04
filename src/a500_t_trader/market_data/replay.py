"""Publish completed historical minute bars through the normalized event path."""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import Any

from . import MinuteBar
from .mode import MarketDataMode, market_data_mode
from .rabbitmq import RabbitMQPublisher


def load_history(path: Path, symbol: str | None = None) -> list[MinuteBar]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("bars"), list):
        raise ValueError("market history must contain a bars array")
    bars: list[MinuteBar] = []
    for row in document["bars"]:
        if not isinstance(row, dict):
            raise ValueError("market history bar must be an object")
        if not row.get("is_completed", False):
            continue
        interval_minutes = int(row["interval_minutes"])
        timestamp = datetime.fromisoformat(str(row["timestamp"]))
        if interval_minutes != 1:
            raise ValueError("market history replay accepts only 1-minute bars")
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("market history bar timestamps must be timezone-aware")
        bar_symbol = str(row.get("symbol", document.get("symbol", "")))
        if symbol is not None and bar_symbol != symbol:
            continue
        bars.append(
            MinuteBar(
                symbol=bar_symbol,
                timestamp=timestamp,
                interval_minutes=interval_minutes,
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(row["volume"]),
                amount=(
                    Decimal(str(row["amount"]))
                    if row.get("amount") is not None
                    else None
                ),
                is_completed=True,
                source="replay",
            )
        )
    bars.sort(key=lambda bar: bar.timestamp)
    if not bars:
        raise ValueError(
            "market history contains no completed bars for the selected symbol"
        )
    return bars


def replay_history(
    bars: list[MinuteBar],
    publish: Any,
    *,
    start_time: datetime | None = None,
    speed: float = 1.0,
    stop: Event | None = None,
    wait: Any = time.sleep,
) -> int:
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("replay speed must be a finite number greater than zero")
    if start_time is not None and (
        start_time.tzinfo is None or start_time.utcoffset() is None
    ):
        raise ValueError("replay start time must be timezone-aware")
    warmup = [
        bar
        for bar in bars
        if start_time is not None and bar.timestamp < start_time
    ]
    active = [
        bar for bar in bars if start_time is None or bar.timestamp >= start_time
    ]
    published = 0
    for bar in warmup:
        if stop is not None and stop.is_set():
            return published
        publish(bar, warmup=True)
        published += 1
    previous: datetime | None = None
    for bar in active:
        if stop is not None and stop.is_set():
            break
        if previous is not None:
            delay = max(0.0, (bar.timestamp - previous).total_seconds() / speed)
            if stop is not None:
                if stop.wait(delay):
                    break
            else:
                wait(delay)
        publish(bar, warmup=False)
        previous = bar.timestamp
        published += 1
    return published


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="a500-market-data-replay")
    parser.add_argument(
        "--file",
        type=Path,
        default=Path("data/market-history/563360_1m_20261102.json"),
    )
    parser.add_argument("--symbol")
    parser.add_argument("--start-time", type=datetime.fromisoformat)
    parser.add_argument("--speed", type=float, default=1.0)
    args = parser.parse_args(argv)
    if market_data_mode() is not MarketDataMode.REPLAY:
        parser.error("replay requires A500_MARKET_DATA_MODE=replay")
    bars = load_history(args.file, args.symbol)
    publisher = RabbitMQPublisher(exchange=MarketDataMode.REPLAY.exchange)
    try:
        replay_history(
            bars,
            lambda bar, *, warmup: publisher.publish(
                bar, replay=True, warmup=warmup
            ),
            start_time=args.start_time,
            speed=args.speed,
        )
    finally:
        publisher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())