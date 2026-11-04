from datetime import datetime
from decimal import Decimal
from threading import Event
from zoneinfo import ZoneInfo

from a500_t_trader.market_data import MinuteBar, Snapshot, Trade
from a500_t_trader.market_data.events import encode_market_event

SHANGHAI = ZoneInfo("Asia/Shanghai")


def events() -> tuple[Snapshot, MinuteBar, Trade]:
    timestamp = datetime(2026, 11, 4, 9, 31, tzinfo=SHANGHAI)
    return (
        Snapshot(
            symbol="563360", timestamp=timestamp, received_at=timestamp,
            last_price=Decimal("1.2345"), previous_close=Decimal("1.2"),
            open=Decimal("1.21"), high=Decimal("1.24"), low=Decimal("1.2"),
            cumulative_volume=100, cumulative_amount=Decimal("123.45"),
            bid_price=Decimal("1.23"), bid_volume=10, ask_price=Decimal("1.24"),
            ask_volume=20, source="tgw",
        ),
        MinuteBar(
            symbol="563360", timestamp=timestamp, interval_minutes=1,
            open=Decimal("1.2"), high=Decimal("1.3"), low=Decimal("1.1"),
            close=Decimal("1.25"), volume=100, amount=Decimal("125"),
            is_completed=True, source="tgw",
        ),
        Trade(
            symbol="563360", timestamp=timestamp, price=Decimal("1.25"),
            quantity=100, amount=Decimal("125"), side=None, source="tgw",
        ),
    )


def test_normalized_events_include_version_event_time_and_nonunique_source_id() -> None:
    timestamp = events()[0].timestamp
    expected_types = ("snapshot", "minute_bar", "trade")
    expected_prices = ("1.2345", "1.25", "1.25")
    for event, event_type, price_field, price in zip(
        events(),
        expected_types,
        ("last_price", "close", "price"),
        expected_prices,
        strict=True,
    ):
        encoded = encode_market_event(event)
        assert encoded["schema_version"] == 1
        assert encoded["event_type"] == event_type
        assert encoded["event_time"] == timestamp.isoformat()
        assert encoded["source_event_id"] is None
        assert encoded["source_event_id_quality"] == "absent"
        assert encoded["symbol"] == "563360"
        data = encoded["data"]
        assert isinstance(data, dict)
        assert data["timestamp"] == timestamp.isoformat()
        assert data[price_field] == price

def test_rabbitmq_publisher_confirms_encoded_event_through_injected_channel() -> None:
    from a500_t_trader.market_data.rabbitmq import RabbitMQPublisher

    class Channel:
        def __init__(self) -> None:
            self.messages: list[tuple[str, str, bytes, bool]] = []
            self.confirmations = 0

        def confirm_delivery(self) -> None:
            self.confirmations += 1

        def basic_publish(
            self,
            exchange: str,
            routing_key: str,
            body: bytes,
            properties: object = None,
            mandatory: bool = False,
        ) -> None:
            self.messages.append((exchange, routing_key, body, mandatory))

    channel = Channel()
    publisher = RabbitMQPublisher(channel=channel, exchange="market.events")
    publisher.publish(events()[0])

    assert channel.confirmations == 1
    assert channel.messages[0][0:2] == ("market.events", "snapshot.563360")
    assert channel.messages[0][3] is True
    assert b'"schema_version":1' in channel.messages[0][2]


def test_collector_retries_factory_and_publishes_events_to_consumer() -> None:
    from collections.abc import Callable

    from a500_t_trader.market_data.collector import (
        CollectorConfig,
        MarketDataCollector,
    )
    from a500_t_trader.market_data.events import MarketEvent

    stop = Event()
    attempts = 0
    observed: list[MarketEvent] = []
    delays: list[float] = []
    states: list[tuple[str, str]] = []

    class Subscription:
        error = None

    class Connection:
        connected = True

        def subscribe(
            self,
            symbol: str,
            event_type: str,
            callback: Callable[[MarketEvent], None],
        ) -> Subscription:
            if event_type == "snapshot":
                callback(events()[0])
            return Subscription()

        def close(self) -> None:
            self.connected = False

    def connect(config: CollectorConfig) -> Connection:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("offline")
        return Connection()

    class Broker:
        def publish(self, event: MarketEvent) -> None:
            observed.append(event)
            stop.set()

    def wait(event: Event, seconds: float) -> bool:
        delays.append(seconds)
        return event.wait(0)

    collector = MarketDataCollector(
        connect,
        CollectorConfig(
            ("563360",),
            retry_initial_seconds=0.1,
            retry_max_seconds=0.2,
            poll_interval_seconds=0.01,
        ),
        Broker(),
        wait=wait,
        on_status=lambda connection, subscription: states.append(
            (connection, subscription)
        ),
    )
    collector.run(stop)

    assert attempts == 2
    assert delays[0] == 0.1
    assert observed == [events()[0]]
    assert collector.connection_state == "stopped"
    assert ("connected", "idle") in states
    assert ("connected", "subscribed") in states
    assert collector.subscription_state == "stopped"


def test_collector_reports_stream_recovered_only_after_first_market_callback() -> None:
    from collections.abc import Callable

    from a500_t_trader.market_data.collector import (
        CollectorConfig,
        MarketDataCollector,
    )
    from a500_t_trader.market_data.events import MarketEvent

    stop = Event()
    states: list[tuple[str, str]] = []
    callbacks: list[Callable[[MarketEvent], None]] = []
    status_when_published: list[str] = []

    class Subscription:
        error = None

    class Connection:
        connected = True

        def subscribe(
            self,
            symbol: str,
            event_type: str,
            callback: Callable[[MarketEvent], None],
        ) -> Subscription:
            callbacks.append(callback)
            return Subscription()

        def close(self) -> None:
            self.connected = False

    class Broker:
        def publish(self, event: MarketEvent) -> None:
            status_when_published.append(collector.subscription_state)
            stop.set()

    def wait(event: Event, seconds: float) -> bool:
        if callbacks:
            callbacks[0](events()[0])
        return event.wait(0)

    collector = MarketDataCollector(
        lambda config: Connection(),
        CollectorConfig(
            ("563360",),
            retry_initial_seconds=0.1,
            retry_max_seconds=0.2,
            poll_interval_seconds=0.01,
        ),
        Broker(),
        wait=wait,
        on_status=lambda connection, subscription: states.append(
            (connection, subscription)
        ),
    )
    collector.run(stop)

    assert ("connected", "awaiting_data") in states
    assert status_when_published == ["awaiting_data"]
    assert ("connected", "subscribed") in states
