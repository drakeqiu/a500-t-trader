"""Command-line startup for an externally configured tgw collector."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Event
from typing import cast

from .collector import CollectorConfig, MarketDataCollector, PushConnection
from .mode import MarketDataMode, market_data_mode
from .rabbitmq import RabbitMQPublisher


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="a500-market-data-collector")
    parser.add_argument(
        "--factory",
        default=os.environ.get("A500_TGW_CONNECTION_FACTORY"),
        help=(
            "external module:callable that connects tgw and returns a push connection"
        ),
    )
    parser.add_argument(
        "--symbols", required=True, help="comma-separated security codes"
    )
    parser.add_argument(
        "--tgw-config", type=Path, help="JSON file passed to the external factory"
    )
    args = parser.parse_args(argv)
    if market_data_mode() is not MarketDataMode.TGW:
        parser.error("collector requires A500_MARKET_DATA_MODE=tgw")
    if not args.factory:
        parser.error("--factory or A500_TGW_CONNECTION_FACTORY is required")
    factory = _load_factory(args.factory)
    options = _load_config(args.tgw_config)
    config = CollectorConfig(
        symbols=tuple(
            symbol.strip() for symbol in args.symbols.split(",") if symbol.strip()
        ),
        connection_options=options,
    )
    publisher = RabbitMQPublisher(exchange=MarketDataMode.TGW.exchange)
    collector = MarketDataCollector(
        factory,
        config,
        publisher,
        on_status=lambda connection, subscription: print(
            f"行情采集状态：连接={connection}，订阅={subscription}",
            file=sys.stderr,
        ),
    )
    stop = Event()
    previous_handlers = {
        signum: signal.signal(signum, lambda *_: stop.set())
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        collector.run(stop)
    finally:
        publisher.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 0


def _load_factory(reference: str) -> Callable[[CollectorConfig], PushConnection]:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("factory must use module:callable syntax")
    factory = getattr(importlib.import_module(module_name), attribute)
    if not callable(factory):
        raise TypeError(f"configured factory {reference!r} is not callable")
    return cast(Callable[[CollectorConfig], PushConnection], factory)


def _load_config(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("tgw config JSON must be an object with string keys")
    return cast(dict[str, object], value)


if __name__ == "__main__":
    raise SystemExit(main())
