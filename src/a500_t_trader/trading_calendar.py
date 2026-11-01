"""Shanghai trading-calendar loading and session-status resolution."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime
from enum import StrEnum
from threading import RLock
from typing import Protocol
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


class CalendarSource(Protocol):
    """Provider of exchange trading dates."""

    def get_calendar(self, market: str = "SH") -> Iterable[date | str]: ...


class SessionState(StrEnum):
    TRADING = "trading"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class AmazingDataCalendar:
    """Vendor adapter for AmazingData ``BaseData.get_calendar``."""

    def __init__(self, base_data: object) -> None:
        self._base_data = base_data

    def get_calendar(self, market: str = "SH") -> tuple[date, ...]:
        dates = self._base_data.get_calendar(market=market)  # type: ignore[attr-defined]
        return tuple(_calendar_date(item) for item in dates)


def default_calendar_source() -> CalendarSource:
    """Create AmazingData's Shanghai adapter when the SDK is importable."""
    try:
        from AmazingData.query_api.base_data import BaseData

        return AmazingDataCalendar(BaseData())
    except Exception:
        return _UnavailableCalendar()


class TradingCalendarService:
    """Cache exchange dates, refreshing at most once per Shanghai-local day."""

    def __init__(
        self,
        source: CalendarSource | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source if source is not None else default_calendar_source()
        self._clock = clock if clock is not None else _now_shanghai
        self._lock = RLock()
        self._dates: frozenset[date] | None = None
        self._last_refresh_date: date | None = None
        self._stale = True
        with self._lock:
            self._refresh_for(self._local_today())

    def session_status(self, session_date: date | None = None) -> tuple[SessionState, bool]:
        """Return (state, stale); never infer sessions from weekday rules."""
        with self._lock:
            today = self._local_today()
            self._refresh_for(today)
            if self._dates is None:
                return SessionState.UNKNOWN, True
            target = today if session_date is None else session_date
            return (
                SessionState.TRADING if target in self._dates else SessionState.CLOSED,
                self._stale,
            )

    def _local_today(self) -> date:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("calendar clock must return a timezone-aware datetime")
        return now.astimezone(SHANGHAI).date()

    def _refresh_for(self, today: date) -> None:
        if self._last_refresh_date == today:
            return
        self._last_refresh_date = today
        try:
            dates = frozenset(_calendar_date(value) for value in self._source.get_calendar(market="SH"))
            if not dates:
                raise ValueError("calendar source returned no trading dates")
        except Exception:
            self._stale = True
        else:
            self._dates = dates
            self._stale = False


def _calendar_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"invalid trading calendar date: {value!r}")


def _now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


class _UnavailableCalendar:
    def get_calendar(self, market: str = "SH") -> Iterable[date]:
        raise RuntimeError("AmazingData calendar is unavailable")
