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


def test_consumer_routes_each_latest_state_to_its_bounded_shared_queue() -> None:
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
    class Channel:
        def basic_ack(self, *, delivery_tag: int) -> None:
            pass

        def basic_nack(self, *, delivery_tag: int, requeue: bool) -> None:
            pass

    channel = Channel()
    consumer.handle_delivery(channel, 3, stale)
    assert observed == []

    class SetupChannel:
        def __init__(self) -> None:
            self.declared: list[tuple[str, bool, dict[str, int | str] | None]] = []
            self.bindings: list[tuple[str, str]] = []
            self.qos = 0

        def exchange_declare(self, **kwargs: object) -> None:
            pass

        def queue_declare(
            self,
            *,
            queue: str,
            durable: bool,
            arguments: dict[str, int | str] | None = None,
        ) -> None:
            self.declared.append((queue, durable, arguments))

        def queue_bind(
            self, *, queue: str, exchange: str, routing_key: str
        ) -> None:
            self.bindings.append((queue, routing_key))

        def basic_qos(self, *, prefetch_count: int) -> None:
            self.qos = prefetch_count

        def basic_consume(self, **kwargs: object) -> None:
            pass

    setup = SetupChannel()
    symbols = ("563360", "510300")
    rabbit = RabbitMQConsumer(
        "signals", observed.append, channel=setup, symbols=symbols
    )
    replica = RabbitMQConsumer(
        "signals", observed.append, channel=setup, symbols=symbols
    )
    assert replica.queues == rabbit.queues
    rabbit.configure()

    state_args = {"x-max-length": 1, "x-overflow": "drop-head"}
    expected_state_queues = [
        (f"market.consumer.signals.{event_type}.{symbol}", True, state_args)
        for event_type in ("snapshot", "minute_bar")
        for symbol in symbols
    ]
    expected_trade_queue = ("market.consumer.signals.trade", True, None)
    assert setup.declared == [*expected_state_queues, expected_trade_queue]
    assert setup.bindings == [
        (f"market.consumer.signals.{event_type}.{symbol}", f"{event_type}.{symbol}")
        for event_type in ("snapshot", "minute_bar")
        for symbol in symbols
    ] + [("market.consumer.signals.trade", "trade.*")]
    assert setup.qos == 1
    audit = RabbitMQConsumer("audit", observed.append, symbols=symbols, channel=setup)
    assert set(audit.queues).isdisjoint(rabbit.queues)


