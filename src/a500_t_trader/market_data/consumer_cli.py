"""命令行启动独立的规范化行情事件消费者。"""

from __future__ import annotations

import argparse
import json
import signal
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from .consumer import RabbitMQConsumer


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="a500-market-data-consumer")
    parser.add_argument(
        "--purpose",
        required=True,
        help="消费用途名称；同用途实例共享队列",
    )
    parser.add_argument(
        "--symbols",
        required=True,
        help="逗号分隔的消费行情代码，必须覆盖采集器发布的代码",
    )
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=30.0,
        help="超过该事件时间年龄的事件只确认、不传给处理器",
    )
    args = parser.parse_args(argv)
    symbols = tuple(symbol.strip() for symbol in args.symbols.split(","))
    if any(not symbol for symbol in symbols):
        parser.error("--symbols must contain non-empty comma-separated symbols")
    if args.max_age_seconds < 0:
        parser.error("--max-age-seconds must not be negative")

    def handle(event: dict[str, Any]) -> None:
        print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)

    consumer = RabbitMQConsumer(
        args.purpose,
        handle,
        symbols=symbols,
        max_age=timedelta(seconds=args.max_age_seconds),
    )
    previous = {
        signum: signal.signal(signum, lambda *_: consumer.stop())
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        consumer.run()
    finally:
        consumer.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
