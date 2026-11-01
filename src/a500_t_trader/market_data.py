"""Vendor-neutral market data models and query service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from .trading_calendar import SessionState, TradingCalendarService

DEFAULT_SYMBOL = "563360"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def normalize_symbol(symbol: str) -> str:
    """Return the six-digit security code without an exchange decoration."""
    normalized = symbol.strip().upper().replace("_", ".")
    for prefix in ("SH.", "SZ.", "SSE.", "SZSE."):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    for suffix in (".SH", ".SZ", ".SSE", ".SZSE"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    if len(normalized) != 6 or not normalized.isdigit():
        raise ValueError(f"invalid symbol: {symbol!r}")
    return normalized


def _shanghai_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("market data timestamps must be timezone-aware")
    return value.astimezone(SHANGHAI)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Point-in-time quote; prices are CNY per share, volume shares, amount CNY."""

    symbol: str
    timestamp: datetime
    received_at: datetime
    name: str | None = None
    last_price: Decimal | None = None
    previous_close: Decimal | None = None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    cumulative_volume: int | None = None
    cumulative_amount: Decimal | None = None
    bid_price: Decimal | None = None
    bid_volume: int | None = None
    ask_price: Decimal | None = None
    ask_volume: int | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "timestamp", _shanghai_time(self.timestamp))
        object.__setattr__(self, "received_at", _shanghai_time(self.received_at))


@dataclass(frozen=True, slots=True)
class MinuteBar:
    """OHLC minute aggregate; prices/amount are CNY, volume is shares."""

    symbol: str
    timestamp: datetime
    interval_minutes: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    amount: Decimal | None = None
    is_completed: bool = True
    source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "timestamp", _shanghai_time(self.timestamp))
        if self.interval_minutes not in (1, 5):
            raise ValueError("interval_minutes must be 1 or 5")


@dataclass(frozen=True, slots=True)
class Trade:
    """One source-reported execution; quantity is shares, monetary values CNY."""

    symbol: str
    timestamp: datetime
    price: Decimal
    quantity: int
    amount: Decimal | None = None
    side: str | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "timestamp", _shanghai_time(self.timestamp))


class CapabilityUnavailableError(RuntimeError):
    """Raised when the current source cannot provide a requested capability."""


class MarketDataSource(Protocol):
    """Replaceable query source; implementations expose no vendor types here."""

    def get_snapshot(self, symbol: str) -> Snapshot | None: ...

    def get_intraday_bars(
        self, symbol: str, interval_minutes: int
    ) -> tuple[MinuteBar, ...]: ...

    def get_history_bars(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        interval_minutes: int,
    ) -> tuple[MinuteBar, ...]: ...

    def get_trades(self, symbol: str) -> tuple[Trade, ...]: ...


class MarketDataService:
    """Unified request/response market data API over an injected source."""

    def __init__(
        self,
        source: MarketDataSource,
        default_symbol: str = DEFAULT_SYMBOL,
        calendar: TradingCalendarService | None = None,
    ) -> None:
        self._source = source
        self._default_symbol = normalize_symbol(default_symbol)
        self._calendar = calendar if calendar is not None else TradingCalendarService()

    def get_snapshot(self, symbol: str | None = None) -> Snapshot | None:
        return self._source.get_snapshot(self._symbol(symbol))

    def get_intraday_bars(
        self, symbol: str | None = None, interval_minutes: int = 1
    ) -> tuple[MinuteBar, ...]:
        if interval_minutes not in (1, 5):
            raise ValueError("interval_minutes must be 1 or 5")
        return self._source.get_intraday_bars(self._symbol(symbol), interval_minutes)

    def get_history_bars(
        self,
        symbol: str | None,
        start_date: date,
        end_date: date,
        interval_minutes: int = 1,
    ) -> tuple[MinuteBar, ...]:
        if interval_minutes not in (1, 5):
            raise ValueError("interval_minutes must be 1 or 5")
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date")
        return self._source.get_history_bars(
            self._symbol(symbol), start_date, end_date, interval_minutes
        )

    def get_trades(self, symbol: str | None = None) -> tuple[Trade, ...]:
        return self._source.get_trades(self._symbol(symbol))

    def session_status(
        self, session_date: date | None = None
    ) -> tuple[SessionState, bool]:
        return self._calendar.session_status(session_date)

    def _symbol(self, symbol: str | None) -> str:
        return self._default_symbol if symbol is None else normalize_symbol(symbol)
