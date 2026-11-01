from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from datetime import datetime
from decimal import Decimal
from threading import Condition, RLock
from typing import Any
from zoneinfo import ZoneInfo

from .market_data import MinuteBar, Snapshot, Trade, normalize_symbol

SHANGHAI = ZoneInfo("Asia/Shanghai")


class TickOverflowError(RuntimeError):
    """Raised when tick events cannot be delivered without loss."""


class Subscription(Iterator[Snapshot | MinuteBar | Trade]):
    """One callback or iterator consumer with bounded event storage."""

    def __init__(
        self,
        event_type: str,
        *,
        symbol: str | None = None,
        callback: Callable[[Any], None] | None = None,
        buffer_size: int = 256,
        on_close: Callable[[Subscription], None] | None = None,
    ) -> None:
        if event_type not in {"snapshot", "bar", "tick"}:
            raise ValueError("event_type must be 'snapshot', 'bar', or 'tick'")
        if buffer_size < 1:
            raise ValueError("buffer_size must be positive")
        self.event_type = event_type
        self.symbol = normalize_symbol(symbol) if symbol is not None else None
        self._callback = callback
        self._buffer_size = buffer_size
        self._on_close = on_close
        self._events: deque[Snapshot | MinuteBar | Trade] = deque()
        self._condition = Condition()
        self._closed = False
        self._error: BaseException | None = None

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def __iter__(self) -> Subscription:
        return self

    def __next__(self) -> Snapshot | MinuteBar | Trade:
        with self._condition:
            while not self._events and not self._closed:
                self._condition.wait()
            if self._error is not None:
                raise self._error
            if self._events:
                return self._events.popleft()
            raise StopIteration

    def publish(self, event: Snapshot | MinuteBar | Trade) -> None:
        with self._condition:
            if self._closed:
                return
            callback = self._callback
            if callback is None:
                if self.event_type in {"snapshot", "bar"}:
                    if len(self._events) >= self._buffer_size:
                        self._events.clear()
                    self._events.append(event)
                elif len(self._events) >= self._buffer_size:
                    self._error = TickOverflowError(
                        "tick buffer overflow; subscription terminated"
                    )
                    self._closed = True
                    self._condition.notify_all()
                    close = self._on_close
                    if close is not None:
                        close(self)
                    return
                else:
                    self._events.append(event)
                self._condition.notify()
        if callback is None:
            return
        try:
            callback(event)
        except Exception as error:
            self.fail(error)

    def fail(self, error: BaseException) -> None:
        with self._condition:
            if self._closed:
                return
            self._error = error
            self._closed = True
            self._condition.notify_all()
        if self._on_close is not None:
            self._on_close(self)

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
        if self._on_close is not None:
            self._on_close(self)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value)
        for fmt in ("%Y%m%d%H%M%S%f", "%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d%H%M%S%f0"):
            try:
                result = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"invalid tgw timestamp: {value!r}")
    return (
        result.replace(tzinfo=SHANGHAI)
        if result.tzinfo is None
        else result.astimezone(SHANGHAI)
    )


def _snapshot(raw: Any, now: datetime) -> Snapshot:
    code = normalize_symbol(str(raw.security_code))
    return Snapshot(
        symbol=code,
        timestamp=_timestamp(raw.orig_time),
        received_at=now,
        last_price=_decimal(raw.last_price),
        previous_close=_decimal(raw.pre_close_price),
        open=_decimal(raw.open_price),
        high=_decimal(raw.high_price),
        low=_decimal(raw.low_price),
        cumulative_volume=int(raw.total_volume_trade),
        cumulative_amount=_decimal(raw.total_value_trade),
        bid_price=_decimal(raw.bid_price[0]),
        bid_volume=int(raw.bid_volume[0]),
        ask_price=_decimal(raw.offer_price[0]),
        ask_volume=int(raw.offer_volume[0]),
        source="tgw",
    )


def _bar(raw: Any, now: datetime, interval: int = 1) -> MinuteBar:
    timestamp = _timestamp(raw.kline_time)
    current_minute = now.astimezone(SHANGHAI).replace(second=0, microsecond=0)
    bar_minute = timestamp.replace(second=0, microsecond=0)
    return MinuteBar(
        symbol=normalize_symbol(str(raw.security_code)),
        timestamp=timestamp,
        interval_minutes=interval,
        open=_decimal(raw.open_price),
        high=_decimal(raw.high_price),
        low=_decimal(raw.low_price),
        close=_decimal(raw.close_price),
        volume=int(raw.volume_trade),
        amount=_decimal(raw.value_trade),
        is_completed=bar_minute < current_minute,
        source="tgw",
    )


def _trade(raw: Any) -> Trade:
    return Trade(
        symbol=normalize_symbol(str(raw.security_code)),
        timestamp=_timestamp(raw.exec_time),
        price=_decimal(raw.exec_price),
        quantity=int(raw.exec_volume),
        amount=_decimal(raw.value_trade),
        side=str(raw.side),
        source="tgw",
    )


class TgwPushProvider:
    """Adapter from tgw push callbacks to normalized, bounded subscriptions."""

    def __init__(
        self, tgw_api: Any, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._api = tgw_api
        self._clock = clock or (lambda: datetime.now(SHANGHAI))
        self._subscriptions: set[Subscription] = set()
        self._items: dict[Subscription, Any] = {}
        self._lock = RLock()
        self._closed = False
        self._spi = _PushSpi(self)

    def subscribe(
        self,
        symbol: str,
        event_type: str,
        callback: Callable[[Any], None] | None = None,
        *,
        buffer_size: int = 256,
    ) -> Subscription:
        with self._lock:
            if self._closed:
                raise RuntimeError("provider is closed")
            code = normalize_symbol(symbol)
            subscription = Subscription(
                event_type,
                symbol=code,
                callback=callback,
                buffer_size=buffer_size,
                on_close=self._remove,
            )
            item = self._make_item(code, event_type)
            self._subscriptions.add(subscription)
            self._items[subscription] = item
            try:
                result = self._api.Subscribe(item, self._spi)
            except Exception:
                self._subscriptions.discard(subscription)
                self._items.pop(subscription, None)
                raise
            if result not in (None, 0):
                self._subscriptions.discard(subscription)
                self._items.pop(subscription, None)
                raise RuntimeError(f"tgw subscription failed: {result}")
            return subscription

    def _make_item(self, code: str, event_type: str) -> Any:
        item = self._api.SubscribeItem()
        item.market = (
            self._api.MarketType.kSSE
            if code.startswith(("5", "6", "9"))
            else self._api.MarketType.kSZSE
        )
        item.security_code = code
        item.category_type = {
            "snapshot": self._api.MDDatatype.kSnapshot,
            "bar": self._api.MDDatatype.k1KLine,
            "tick": self._api.MDDatatype.kTickExecution,
        }[event_type]
        return item

    def _remove(self, subscription: Subscription) -> None:
        with self._lock:
            item = self._items.pop(subscription, None)
            self._subscriptions.discard(subscription)
            if item is not None and not self._closed:
                result = self._api.UnSubscribe(item, self._spi)
                if result not in (None, 0):
                    subscription.fail(RuntimeError(f"tgw unsubscribe failed: {result}"))

    def _dispatch(self, kind: str, raw: Any, error_message: str = "") -> None:
        with self._lock:
            targets = tuple(s for s in self._subscriptions if s.event_type == kind)
        if error_message:
            for sub in targets:
                sub.fail(RuntimeError(f"tgw {kind} push failed: {error_message}"))
            return
        try:
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                now = now.replace(tzinfo=SHANGHAI)
            now = now.astimezone(SHANGHAI)
            event: Snapshot | MinuteBar | Trade
            if kind == "snapshot":
                event = _snapshot(raw, now)
            elif kind == "bar":
                event = _bar(raw, now)
            elif kind == "tick":
                event = _trade(raw)
            else:
                raise ValueError(f"unsupported push event type: {kind}")
        except Exception as conversion_error:
            for sub in targets:
                sub.fail(conversion_error)
            return
        for sub in targets:
            if event.symbol == sub.symbol:
                sub.publish(event)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            items = tuple(self._items.values())
            subscriptions = tuple(self._subscriptions)
            self._items.clear()
            self._subscriptions.clear()
        if items:
            self._api.UnSubscribe(list(items), self._spi)
        for sub in subscriptions:
            sub.close()
        self._api.Close()


class _PushSpi:
    def __init__(self, provider: TgwPushProvider) -> None:
        self._provider = provider

    def OnMDSnapshot(self, data: Any, error: str = "") -> None:
        self._provider._dispatch("snapshot", data, error)

    def OnKLine(self, data: Any, kline_type: Any = None, error: str = "") -> None:
        self._provider._dispatch("bar", data, error)

    def OnMDTickExecution(self, data: Any, error: str = "") -> None:
        self._provider._dispatch("tick", data, error)
