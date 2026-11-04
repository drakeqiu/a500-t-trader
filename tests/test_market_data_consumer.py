from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from a500_t_trader.market_data.consumer import MarketEventConsumer, RabbitMQConsumer

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_consumer_acknowledges_fresh_json_and_nacks_handler_failure() -> None:
    now = datetime(2026, 11, 4, 9, 31, tzinfo=SHANGHAI)
    event = {
        "schema_version": 1,
        "event_type": "snapshot",
        "event_time": now.isoformat(),
        "source_event_id": None,
        "source_event_id_quality": "absent",
        "symbol": "563360",
        "data": {"symbol": "563360", "timestamp": now.isoformat()},
    }
    class Channel:
        def __init__(self) -> None:
            self.acks: list[int] = []
            self.nacks: list[tuple[int, bool]] = []

        def basic_ack(self, delivery_tag: int) -> None:
            self.acks.append(delivery_tag)

        def basic_nack(self, delivery_tag: int, requeue: bool) -> None:
            self.nacks.append((delivery_tag, requeue))

    observed: list[dict[str, object]] = []
    consumer = MarketEventConsumer(
        "signals", observed.append, max_age=timedelta(seconds=5), clock=lambda: now
    )
    channel = Channel()
    consumer.handle_delivery(channel, 1, event)
    assert channel.acks == [1]
    consumer.handle_delivery(channel, 4, event)
    assert channel.acks == [1, 4]
    assert observed == [event, event]

    failed = MarketEventConsumer(
        "signals",
        lambda _: (_ for _ in ()).throw(RuntimeError("fail")),
        clock=lambda: now,
    )
    failed.handle_delivery(channel, 2, event)
    assert channel.nacks == [(2, True)]


def test_consumer_drops_stale_message_and_configures_shared_durable_queue() -> None:
    now = datetime(2026, 11, 4, 9, 31, tzinfo=SHANGHAI)
    stale = {
        "schema_version": 1,
        "event_type": "trade",
        "event_time": (now - timedelta(seconds=10)).isoformat(),
        "source_event_id": None,
        "source_event_id_quality": "absent",
        "symbol": "563360",
        "data": {},
    }
    observed: list[dict[str, object]] = []
    consumer = MarketEventConsumer(
        "trades", observed.append, max_age=timedelta(seconds=5), clock=lambda: now
    )
    channel = type(
        "Channel",
        (),
        {
            "basic_ack": lambda self, tag: None,
            "basic_nack": lambda self, tag, requeue: None,
        },
    )()
    consumer.handle_delivery(channel, 3, stale)
    assert observed == []

    class SetupChannel:
        def __init__(self) -> None:
            self.declared: list[tuple[str, bool]] = []
            self.bindings: list[str] = []
            self.qos = 0
        def exchange_declare(self, **kwargs: object) -> None:
            pass

        def queue_declare(self, *, queue: str, durable: bool) -> None:
            self.declared.append((queue, durable))

        def queue_bind(
            self, *, queue: str, exchange: str, routing_key: str
        ) -> None:
            self.bindings.append(queue)

        def basic_qos(self, *, prefetch_count: int) -> None:
            self.qos = prefetch_count

        def basic_consume(self, **kwargs: object) -> None:
            pass

    setup = SetupChannel()
    rabbit = RabbitMQConsumer("signals", observed.append, channel=setup)
    replica = RabbitMQConsumer("signals", observed.append, channel=setup)
    assert replica.queue == rabbit.queue
    rabbit.configure()
    assert setup.declared == [("market.consumer.signals", True)]
    assert setup.bindings == ["market.consumer.signals"]
    assert setup.qos == 1


def test_market_data_service_query_interface_remains_separate() -> None:
    from a500_t_trader.market_data import MarketDataService

    assert callable(MarketDataService)
