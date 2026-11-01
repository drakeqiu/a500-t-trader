from datetime import date, datetime
from zoneinfo import ZoneInfo

from a500_t_trader.trading_calendar import (
    AmazingDataCalendar,
    SessionState,
    TradingCalendarService,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


class DeterministicCalendar:
    def __init__(self, dates: tuple[date, ...] = ()) -> None:
        self.dates = dates
        self.calls = 0
        self.error: Exception | None = None

    def get_calendar(self, market: str = "SH") -> tuple[date, ...]:
        self.calls += 1
        if self.error:
            raise self.error
        assert market == "SH"
        return self.dates


def test_calendar_loads_at_startup_and_reports_known_trading_dates() -> None:
    provider = DeterministicCalendar((date(2026, 11, 2),))

    def clock() -> datetime:
        return datetime(2026, 11, 2, 9, tzinfo=SHANGHAI)

    calendar = TradingCalendarService(provider, clock=clock)

    assert provider.calls == 1
    assert calendar.session_status() == (SessionState.TRADING, False)
    assert calendar.session_status(date(2026, 11, 3)) == (SessionState.CLOSED, False)
    assert provider.calls == 1


def test_calendar_refreshes_once_each_shanghai_local_date() -> None:
    provider = DeterministicCalendar((date(2026, 11, 2), date(2026, 11, 3)))
    now = [datetime(2026, 11, 2, 23, 59, tzinfo=SHANGHAI)]
    calendar = TradingCalendarService(provider, clock=lambda: now[0])

    assert calendar.session_status() == (SessionState.TRADING, False)
    now[0] = datetime(2026, 11, 3, 0, 1, tzinfo=SHANGHAI)
    assert calendar.session_status() == (SessionState.TRADING, False)
    assert calendar.session_status() == (SessionState.TRADING, False)
    assert provider.calls == 2


def test_refresh_failure_retains_last_valid_calendar_as_stale() -> None:
    provider = DeterministicCalendar((date(2026, 11, 2),))
    now = [datetime(2026, 11, 2, tzinfo=SHANGHAI)]
    calendar = TradingCalendarService(provider, clock=lambda: now[0])
    provider.error = OSError("calendar unavailable")
    now[0] = datetime(2026, 11, 3, tzinfo=SHANGHAI)

    assert calendar.session_status(date(2026, 11, 2)) == (SessionState.TRADING, True)
    assert calendar.session_status(date(2026, 11, 4)) == (SessionState.CLOSED, True)
    assert provider.calls == 2


def test_no_valid_calendar_means_unknown_not_inferred_weekday_status() -> None:
    provider = DeterministicCalendar()
    provider.error = OSError("calendar unavailable")
    calendar = TradingCalendarService(
        provider, clock=lambda: datetime(2026, 11, 2, tzinfo=SHANGHAI)
    )

    assert calendar.session_status() == (SessionState.UNKNOWN, True)
    assert calendar.session_status(date(2026, 11, 7)) == (SessionState.UNKNOWN, True)
    assert provider.calls == 1


def test_amazingdata_adapter_uses_shanghai_market() -> None:
    class BaseData:
        def __init__(self) -> None:
            self.market: str | None = None

        def get_calendar(self, market: str = "SH") -> list[str]:
            self.market = market
            return ["2026-11-02"]

    base_data = BaseData()
    provider = AmazingDataCalendar(base_data)

    assert provider.get_calendar() == (date(2026, 11, 2),)
    assert base_data.market == "SH"


def test_invalid_calendar_result_is_a_failed_refresh() -> None:
    provider = DeterministicCalendar((date(2026, 11, 2),))
    provider.dates = ("not-a-date",)  # type: ignore[assignment]
    calendar = TradingCalendarService(
        provider, clock=lambda: datetime(2026, 11, 2, tzinfo=SHANGHAI)
    )

    assert calendar.session_status() == (SessionState.UNKNOWN, True)
