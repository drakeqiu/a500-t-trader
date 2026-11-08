import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from a500_t_trader.market_data import MinuteBar
from a500_t_trader.market_data.consumer import MarketEventConsumer
from a500_t_trader.market_data.mode import MarketDataMode, market_data_mode
from a500_t_trader.market_data.rabbitmq import RabbitMQPublisher
from a500_t_trader.market_data.replay import load_history, replay_history

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_replay_loads_completed_bars_and_start_time_warms_up_without_delay(
    tmp_path: Path,
) -> None:
    history = {
        "symbol": "563360",
        "bars": [
            {
                "timestamp": f"2026-11-02T09:{minute:02d}:00+08:00",
                "interval_minutes": 1,
                "open": "1.0",
                "high": "1.1",
                "low": "0.9",
                "close": "1.0",
                "volume": 1,
                "is_completed": minute != 32,
            }
            for minute in (30, 31, 32, 33)
        ],
    }
    source = tmp_path / "history.json"
    source.write_text(json.dumps(history), encoding="utf-8")
    bars = load_history(source)
    published: list[tuple[MinuteBar, bool]] = []
    waits: list[float] = []

    count = replay_history(
        bars,
        lambda bar, *, warmup: published.append((bar, warmup)),
        start_time=datetime(2026, 11, 2, 9, 31, tzinfo=SHANGHAI),
        speed=2,
        wait=waits.append,
    )

    assert count == 3
    assert [warmup for _, warmup in published] == [True, False, False]
    assert [bar.timestamp.minute for bar, _ in published] == [30, 31, 33]
    assert waits == [60.0]


def test_replay_event_preserves_market_time_and_uses_replay_clock() -> None:
    timestamp = datetime(2026, 11, 2, 9, 30, tzinfo=SHANGHAI)
    bar = MinuteBar(
        "563360",
        timestamp,
        1,
        Decimal("1"),
        Decimal("1"),
        Decimal("1"),
        Decimal("1"),
        1,
        source="replay",
    )

    class Channel:
        message: dict[str, Any]

        def basic_publish(self, **kwargs: Any) -> None:
            self.message = kwargs

    channel = Channel()
    RabbitMQPublisher(channel=channel, exchange=MarketDataMode.REPLAY.exchange).publish(
        bar, replay=True, warmup=True
    )
    event = json.loads(channel.message["body"])
    observed: list[dict[str, object]] = []

    class Ack:
        def basic_ack(self, **kwargs: object) -> None:
            pass

        def basic_nack(self, **kwargs: object) -> None:
            raise AssertionError("valid replay event was rejected")

    MarketEventConsumer(
        "signals",
        observed.append,
        clock=lambda: datetime.now(SHANGHAI) + timedelta(seconds=1),
    ).handle_delivery(Ack(), 1, event)
    assert event["event_time"] == timestamp.isoformat()
    assert datetime.fromisoformat(event["replay_time"]).tzinfo is not None
    assert event["source"] == "replay"
    assert event["warmup"] is True
    assert observed == [event]


def test_market_data_mode_requires_explicit_valid_value() -> None:
    with pytest.raises(ValueError, match="MARKET_DATA_MODE"):
        market_data_mode("")
    assert market_data_mode("tgw") is MarketDataMode.TGW
    assert market_data_mode("replay") is MarketDataMode.REPLAY
