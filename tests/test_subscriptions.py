from datetime import datetime
from decimal import Decimal
from threading import Thread
from zoneinfo import ZoneInfo

class FakeTgw:
    class MarketType:
        kSSE = 1
        kSZSE = 2

    class MDDatatype:
        kSnapshot = 10
        k1KLine = 11
        kTickExecution = 12

    class SubscribeItem:
        pass

    def __init__(self) -> None:
        self.calls = []

    def Subscribe(self, item, spi):
        self.calls.append(("subscribe", item, spi))
        return 0

    def UnSubscribe(self, item, spi):
        self.calls.append(("unsubscribe", item, spi))
        return 0

    def Close(self):
        self.calls.append(("close",))


def test_provider_uses_tgw_and_cancels_subscription_before_close() -> None:
    from types import SimpleNamespace

    from a500_t_trader.push_provider import TgwPushProvider

    api = FakeTgw()
    provider = TgwPushProvider(api, clock=lambda: datetime(2026, 11, 2, 9, 30, tzinfo=SHANGHAI))
    sub = provider.subscribe("563360.SH", "snapshot", buffer_size=2)
    spi = api.calls[0][2]
    spi.OnMDSnapshot(SimpleNamespace(
        security_code="563360", orig_time=20261102093000,
        last_price=1.23, pre_close_price=1.2, open_price=1.21, high_price=1.24,
        low_price=1.2, total_volume_trade=20, total_value_trade=24.6,
        bid_price=[1.22], bid_volume=[3], offer_price=[1.23], offer_volume=[4],
    ))
    assert next(sub).last_price == Decimal("1.23")
    sub.close()
    provider.close()

    assert [call[0] for call in api.calls] == ["subscribe", "unsubscribe", "close"]


def test_callback_consumer_receives_events() -> None:
    received = []
    sub = Subscription("snapshot", callback=received.append)
    sub.publish(snapshot("1.1"))
    assert received == [snapshot("1.1")]

import pytest

from a500_t_trader.market_data import MinuteBar, Snapshot, Trade
from a500_t_trader.push_provider import Subscription, TickOverflowError

SHANGHAI = ZoneInfo("Asia/Shanghai")


def snapshot(price: str) -> Snapshot:
    now = datetime(2026, 11, 2, 9, 30, tzinfo=SHANGHAI)
    return Snapshot("563360", now, now, last_price=Decimal(price))


def trade(second: int) -> Trade:
    return Trade(
        "563360",
        datetime(2026, 11, 2, 9, 30, second, tzinfo=SHANGHAI),
        Decimal("1.23"),
        1,
    )


def test_iterator_coalesces_slow_snapshot_consumer_to_latest_value() -> None:
    sub = Subscription("snapshot", buffer_size=1)
    sub.publish(snapshot("1.1"))
    sub.publish(snapshot("1.2"))

    assert next(sub) == snapshot("1.2")


def test_iterator_coalesces_slow_bar_consumer_to_latest_value() -> None:
    sub = Subscription("bar", buffer_size=1)
    bar1 = MinuteBar("563360", datetime(2026, 11, 2, 9, 30, tzinfo=SHANGHAI), 1, Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"), 1)
    bar2 = MinuteBar("563360", bar1.timestamp, 1, Decimal("1"), Decimal("2"), Decimal("1"), Decimal("2"), 2)
    sub.publish(bar1)
    sub.publish(bar2)

    assert next(sub) == bar2


def test_tick_buffer_overflow_terminates_iterator_with_explicit_error() -> None:
    sub = Subscription("tick", buffer_size=1)
    sub.publish(trade(1))
    sub.publish(trade(2))

    with pytest.raises(TickOverflowError, match="tick buffer overflow"):
        next(sub)
    assert sub.closed


def test_callback_consumer_receives_events_and_unsubscribe_closes_stream() -> None:
    received = []
    sub = Subscription("snapshot", callback=received.append)
    sub.publish(snapshot("1.1"))
    sub.close()

    assert received == [snapshot("1.1")]
    assert sub.closed
    with pytest.raises(StopIteration):
        next(sub)
