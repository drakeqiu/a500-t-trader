from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from a500_t_trader.market_data import (
    CapabilityUnavailableError,
    MarketDataService,
    MinuteBar,
    Snapshot,
    Trade,
)
from a500_t_trader.trading_calendar import SessionState, TradingCalendarService


SHANGHAI = ZoneInfo("Asia/Shanghai")


class DeterministicSource:
    def __init__(self) -> None:
        self.snapshot: Snapshot | None = None
        self.bars: tuple[MinuteBar, ...] = ()
        self.trades: tuple[Trade, ...] = ()
        self.calls: list[tuple[str, str]] = []
        self.unavailable: set[str] = set()

    def get_snapshot(self, symbol: str) -> Snapshot | None:
        self.calls.append(("snapshot", symbol))
        if "snapshot" in self.unavailable:
            raise CapabilityUnavailableError("snapshot unavailable")
        return self.snapshot

    def get_intraday_bars(self, symbol: str, interval_minutes: int) -> tuple[MinuteBar, ...]:
        self.calls.append(("bars", symbol))
        if "bars" in self.unavailable:
            raise CapabilityUnavailableError("bars unavailable")
        return self.bars

    def get_trades(self, symbol: str) -> tuple[Trade, ...]:
        self.calls.append(("trades", symbol))
        if "trades" in self.unavailable:
            raise CapabilityUnavailableError("trades unavailable")
        return self.trades


class FixedCalendar:
    def get_calendar(self, market: str = "SH") -> tuple[date, ...]:
        assert market == "SH"
        return (date(2026, 11, 2),)


def _service(source: DeterministicSource) -> MarketDataService:
    return MarketDataService(source, calendar=TradingCalendarService(FixedCalendar()))


def test_service_normalizes_requested_symbol_and_preserves_optional_snapshot_fields() -> None:
    source = DeterministicSource()
    expected_time = datetime(2026, 11, 1, 9, 30, tzinfo=SHANGHAI)
    source.snapshot = Snapshot(
        symbol="563360",
        timestamp=expected_time,
        received_at=datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc),
        last_price=Decimal("1.2345"),
    )
    service = _service(source)

    actual = service.get_snapshot("sh.563360")

    assert actual == source.snapshot
    assert actual is not None
    assert actual.symbol == "563360"
    assert actual.last_price == Decimal("1.2345")
    assert actual.name is None
    assert actual.bid_price is None
    assert actual.timestamp.tzinfo == SHANGHAI
    assert actual.received_at.astimezone(SHANGHAI).hour == 9
    assert source.calls == [("snapshot", "563360")]


def test_service_defaults_symbol_and_returns_empty_bar_and_trade_results() -> None:
    source = DeterministicSource()
    service = MarketDataService(source)

    assert service.get_snapshot() is None
    assert service.get_intraday_bars() == ()
    assert service.get_trades() == ()
    assert source.calls == [("snapshot", "563360"), ("bars", "563360"), ("trades", "563360")]


def test_service_preserves_units_precision_and_optional_bar_and_trade_fields() -> None:
    source = DeterministicSource()
    timestamp = datetime(2026, 11, 1, 9, 31, tzinfo=SHANGHAI)
    bar = MinuteBar(
        symbol="563360",
        timestamp=timestamp,
        interval_minutes=1,
        open=Decimal("1.2300"),
        high=Decimal("1.2500"),
        low=Decimal("1.2200"),
        close=Decimal("1.2400"),
        volume=1200,
        amount=Decimal("1488.00"),
        is_completed=False,
    )
    trade = Trade(
        symbol="563360",
        timestamp=timestamp,
        price=Decimal("1.2400"),
        quantity=100,
    )
    source.bars = (bar,)
    source.trades = (trade,)

    assert _service(source).get_intraday_bars() == (bar,)
    assert _service(source).get_trades() == (trade,)
    assert bar.interval_minutes == 1
    assert bar.is_completed is False
    assert bar.amount == Decimal("1488.00")
    assert trade.amount is None
    assert trade.side is None

def test_models_normalize_exchange_suffix_and_convert_times_to_shanghai() -> None:
    timestamp = datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc)

    bar = MinuteBar(
        symbol="563360.SH",
        timestamp=timestamp,
        interval_minutes=5,
        open=Decimal("1.2300"),
        high=Decimal("1.2500"),
        low=Decimal("1.2200"),
        close=Decimal("1.2400"),
        volume=1200,
    )
    trade = Trade(
        symbol="SZ.563360",
        timestamp=timestamp,
        price=Decimal("1.2400"),
        quantity=100,
    )

    assert bar.symbol == trade.symbol == "563360"
    assert bar.timestamp == trade.timestamp == datetime(
        2026, 11, 1, 9, 30, tzinfo=SHANGHAI
    )
    assert bar.open == Decimal("1.2300")
    assert bar.amount is None


def test_models_reject_naive_timestamps_and_unsupported_bar_intervals() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Trade(
            symbol="563360",
            timestamp=datetime(2026, 11, 1, 9, 30),
            price=Decimal("1.24"),
            quantity=100,
        )

    with pytest.raises(ValueError, match="interval_minutes"):
        MinuteBar(
            symbol="563360",
            timestamp=datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc),
            interval_minutes=3,
            open=Decimal("1.23"),
            high=Decimal("1.25"),
            low=Decimal("1.22"),
            close=Decimal("1.24"),
            volume=1200,
        )

def test_unavailable_capability_is_distinct_from_empty_result() -> None:
    source = DeterministicSource()
    service = _service(source)
    source.unavailable.add("trades")

    with pytest.raises(CapabilityUnavailableError, match="trades unavailable"):
        service.get_trades()


def test_market_data_service_exposes_injected_calendar_status() -> None:
    service = _service(DeterministicSource())

    assert service.session_status(date(2026, 11, 2)) == (SessionState.TRADING, False)
