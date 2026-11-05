"""Independent normalized-market-event consumer for V1 signal production."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from redis import Redis

from .cli.live_signals import _aggregate_five_minute, _evaluate
from .market_data import MinuteBar
from .strategy.intraday_v1_config import (
    STRATEGY_ID,
    LiveSignalConfig,
    load_live_signal_config,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


class SignalCalculator:
    """Evaluate completed bars once per strategy identity and market event time."""

    def __init__(
        self,
        redis: Redis,
        config: LiveSignalConfig,
        evaluator: Callable[
            [str, MinuteBar, list[MinuteBar], LiveSignalConfig, datetime],
            dict[str, object],
        ] = _evaluate,
    ) -> None:
        self.redis = redis
        self.config = config
        self.evaluator = evaluator
        self.buckets: dict[str, list[MinuteBar]] = defaultdict(list)
        self.histories: dict[str, list[MinuteBar]] = defaultdict(list)
        for symbol in config.etfs:
            cached = redis.get(f"a500:signals:history:{symbol}")
            if cached is not None:
                self.histories[symbol] = [_bar(row) for row in json.loads(cached)]

    def handle(self, event: dict[str, Any]) -> None:
        if event.get("event_type") != "minute_bar":
            return
        raw = event.get("data")
        if (
            not isinstance(raw, dict)
            or raw.get("completed", raw.get("is_completed")) is False
        ):
            return
        symbol = str(event.get("symbol", ""))
        if symbol not in self.config.etfs:
            return
        bar = _bar(raw)
        if bar.interval_minutes == 1:
            bucket = self.buckets[symbol]
            start = bar.timestamp.replace(
                minute=bar.timestamp.minute - bar.timestamp.minute % 5,
                second=0,
                microsecond=0,
            )
            if bucket and bucket[0].timestamp != start:
                bucket.clear()
            if any(item.timestamp == bar.timestamp for item in bucket):
                return
            bucket.append(bar)
            if bar.timestamp.minute % 5 != 4:
                return
            expected = [start + timedelta(minutes=index) for index in range(5)]
            if len(bucket) != 5 or [item.timestamp for item in bucket] != expected:
                bucket.clear()
                return
            bar = _aggregate_five_minute(start, bucket)
            bucket.clear()
        identity = f"{STRATEGY_ID}:{self.config.fingerprint}"
        event_time = bar.timestamp.astimezone(UTC).isoformat()
        dedupe = f"a500:signals:dedupe:{identity}:{symbol}:{event_time}"
        if self.redis.get(dedupe) is not None:
            return
        claim = f"{dedupe}:claim"
        if not self.redis.set(claim, "1", nx=True, ex=60):
            return
        try:
            if self.redis.get(dedupe) is not None:
                return
            history = list(self.histories[symbol])
            result = self.evaluator(
                symbol,
                bar,
                history,
                self.config,
                datetime.now(SHANGHAI),
            )
            result.setdefault("schema_version", 1)
            if not history or history[-1].timestamp != bar.timestamp:
                history.append(bar)
                del history[:-240]
            event_type = str(result.get("direction") or result.get("status", "WAIT"))
            pipe = self.redis.pipeline(transaction=True)
            pipe.set(dedupe, "1", ex=60 * 60 * 24)
            pipe.set(
                f"a500:signals:state:{symbol}",
                json.dumps(result, ensure_ascii=False, default=str),
            )
            pipe.set(
                f"a500:signals:history:{symbol}",
                json.dumps([_bar_payload(item) for item in history]),
            )
            if event_type in {"BUY", "SELL", "BUYBACK", "TAKE_PROFIT"}:
                pipe.xadd(
                    "a500:signals:events",
                    {
                        "symbol": symbol,
                        "type": event_type,
                        "data": json.dumps(result, ensure_ascii=False, default=str),
                    },
                )
                cutoff = int((datetime.now().timestamp() - 24 * 60 * 60) * 1000)
                pipe.xtrim(
                    "a500:signals:events",
                    minid=f"{cutoff}-0",
                    approximate=False,
                )
            pipe.execute()
            self.histories[symbol] = history
            if event_type not in {"BUY", "SELL", "BUYBACK", "TAKE_PROFIT"}:
                from .signal_monitor import UPDATES_CHANNEL

                self.redis.publish(
                    UPDATES_CHANNEL,
                    json.dumps(result, ensure_ascii=False, default=str),
                )
        finally:
            self.redis.delete(claim)


def run_signal_calculator(
    *, purpose: str = "signal-calculator", max_age: timedelta = timedelta(seconds=30)
) -> None:
    from .market_data.consumer import RabbitMQConsumer

    redis = Redis.from_url(
        os.environ.get("A500_REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
    )
    config = load_live_signal_config()
    if not config.etfs:
        raise ValueError("signal calculator requires configured ETF symbols")
    calculator = SignalCalculator(redis, config)
    consumer = RabbitMQConsumer(purpose, calculator.handle, max_age=max_age)
    try:
        consumer.run()
    finally:
        consumer.close()
        redis.close()


def _bar(data: dict[str, Any]) -> MinuteBar:
    timestamp = datetime.fromisoformat(str(data["timestamp"]))
    return MinuteBar(
        symbol=str(data["symbol"]),
        timestamp=timestamp,
        interval_minutes=int(data["interval_minutes"]),
        open=Decimal(str(data["open"])),
        high=Decimal(str(data["high"])),
        low=Decimal(str(data["low"])),
        close=Decimal(str(data["close"])),
        volume=int(data["volume"]),
        amount=None if data.get("amount") is None else Decimal(str(data["amount"])),
        is_completed=bool(data.get("is_completed", data.get("completed", True))),
        source=str(data.get("source", "normalized")),
    )

def _bar_payload(bar: MinuteBar) -> dict[str, Any]:
    return {
        "symbol": bar.symbol,
        "timestamp": bar.timestamp.isoformat(),
        "interval_minutes": bar.interval_minutes,
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": bar.volume,
        "amount": None if bar.amount is None else str(bar.amount),
        "is_completed": bar.is_completed,
        "source": bar.source,
    }
