from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from a500_t_trader.market_data import MinuteBar, Snapshot, Trade
from a500_t_trader.market_data.events import MarketEvent
from a500_t_trader.market_data.recovery import LatestStatePublisher

SHANGHAI = ZoneInfo("Asia/Shanghai")


def quote(price: str, minute: int = 31, symbol: str = "563360") -> Snapshot:
    timestamp = datetime(2026, 11, 4, 9, minute, tzinfo=SHANGHAI)
    return Snapshot(
        symbol=symbol,
        timestamp=timestamp,
        received_at=timestamp,
        last_price=Decimal(price),
        previous_close=Decimal("1"),
        open=Decimal("1"),
        high=Decimal(price),
        low=Decimal("1"),
        cumulative_volume=1,
        cumulative_amount=Decimal(price),
    )


def bar(price: str, minute: int = 31) -> MinuteBar:
    timestamp = datetime(2026, 11, 4, 9, minute, tzinfo=SHANGHAI)
    return MinuteBar(
        symbol="563360",
        timestamp=timestamp,
        interval_minutes=1,
        open=Decimal(price),
        high=Decimal(price),
        low=Decimal(price),
        close=Decimal(price),
        volume=1,
        amount=Decimal(price),
    )


def trade() -> Trade:
    timestamp = datetime(2026, 11, 4, 9, 31, tzinfo=SHANGHAI)
    return Trade(
        symbol="563360",
        timestamp=timestamp,
        price=Decimal("1"),
        quantity=1,
        amount=Decimal("1"),
        side=None,
        source="tgw",
    )


def test_broker_failure_keeps_only_latest_snapshot_and_bar_then_flushes() -> None:

    class Broker:
        available = False
        messages: list[MarketEvent] = []

        def publish(self, event: MarketEvent) -> None:
            if not self.available:
                raise ConnectionError("broker unavailable")
            self.messages.append(event)

    broker = Broker()
    publisher = LatestStatePublisher(broker, symbols=("563360",))
    publisher.publish(quote("1.1"))
    publisher.publish(bar("1.1"))
    publisher.publish(quote("1.2", 32))
    publisher.publish(bar("1.2", 32))
    publisher.publish(trade())
    assert publisher.pending_count == 2
    broker.available = True
    publisher.publish(quote("1.3", 33))
    assert broker.messages == [
        quote("1.2", 32),
        bar("1.2", 32),
        quote("1.3", 33),
    ]
    assert publisher.pending_count == 0


def test_recovery_state_is_bounded_by_configured_symbols_and_process_lifetime() -> None:

    class Broker:
        def publish(self, event: MarketEvent) -> None:
            raise ConnectionError("broker unavailable")

    first_process = LatestStatePublisher(Broker(), symbols=("563360",))
    first_process.publish(quote("1.1"))
    assert first_process.pending_count == 1
    restarted_process = LatestStatePublisher(Broker(), symbols=("563360",))
    assert restarted_process.pending_count == 0
    try:
        first_process.publish(quote("1.2", symbol="510300"))
    except ValueError:
        pass
    else:
        raise AssertionError("events outside configured symbols must be rejected")
