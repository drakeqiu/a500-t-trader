"""AmazingData request/response adapter for normalized market data."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from .. import (
    CapabilityUnavailableError,
    DailyBar,
    MinuteBar,
    Snapshot,
    Trade,
    normalize_symbol,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


class AmazingDataProvider:
    """Logged-in AmazingData provider for request/response market data."""

    def __init__(
        self,
        username: str,
        password: str,
        host: str,
        port: int,
        *,
        login_fn: Callable[..., bool] | None = None,
        market_data: Any | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        if login_fn is None:
            from AmazingData.login.tgw_login import login  # type: ignore[import-untyped]

            login_fn = login
        self._clock = clock
        try:
            authenticated = login_fn(
                username, password, host, port, api_mode="kInternetMode"
            )
        except Exception as error:
            raise CapabilityUnavailableError(
                f"AmazingData login failed: {error}"
            ) from error
        if not authenticated:
            raise CapabilityUnavailableError("AmazingData login was rejected")
        if market_data is None:
            try:
                market_data = _make_market_data()
            except Exception as error:
                raise CapabilityUnavailableError(
                    f"AmazingData market-data initialization failed: {error}"
                ) from error
        self._market_data = market_data

    def get_snapshot(self, symbol: str) -> Snapshot:
        code = normalize_symbol(symbol)
        now = _local_time(self._clock())
        session_date = int(now.strftime("%Y%m%d"))
        try:
            raw = self._market_data.query_snapshot(
                [_vendor_symbol(code)], session_date, session_date
            )
        except Exception as error:
            raise CapabilityUnavailableError(
                f"AmazingData snapshot query unavailable or unauthorized: {error}"
            ) from error
        rows = _snapshot_rows(raw, code, session_date)
        if not rows:
            raise CapabilityUnavailableError(
                f"AmazingData snapshot query returned no data for {code}"
            )
        row = rows[-1]
        try:
            timestamp = _vendor_time(row["trade_time"])
            snapshot = Snapshot(
                symbol=code,
                timestamp=timestamp,
                received_at=now,
                name=_optional_string(row.get("name")),
                last_price=_optional_decimal(row.get("last")),
                previous_close=_optional_decimal(row.get("pre_close")),
                open=_optional_decimal(row.get("open")),
                high=_optional_decimal(row.get("high")),
                low=_optional_decimal(row.get("low")),
                cumulative_volume=_optional_int(row.get("volume")),
                cumulative_amount=_optional_decimal(row.get("amount")),
                bid_price=_optional_decimal(row.get("bid_price1")),
                bid_volume=_optional_int(row.get("bid_volume1")),
                ask_price=_optional_decimal(row.get("ask_price1")),
                ask_volume=_optional_int(row.get("ask_volume1")),
                source="AmazingData",
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise CapabilityUnavailableError(
                f"AmazingData snapshot query returned invalid data for {code}: {error}"
            ) from error
        return snapshot

    def get_intraday_bars(
        self, symbol: str, interval_minutes: int
    ) -> tuple[MinuteBar, ...]:
        today = _local_time(self._clock()).date()
        return self.get_history_bars(symbol, today, today, interval_minutes)

    def get_history_bars(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        interval_minutes: int,
    ) -> tuple[MinuteBar, ...]:
        if interval_minutes not in (1, 5):
            raise ValueError("interval_minutes must be 1 or 5")
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date")
        code = normalize_symbol(symbol)
        period = _period_value(interval_minutes)
        try:
            raw = self._market_data.query_kline(
                [_vendor_symbol(code)],
                int(start_date.strftime("%Y%m%d")),
                int(end_date.strftime("%Y%m%d")),
                period,
            )
        except Exception as error:
            raise CapabilityUnavailableError(
                f"AmazingData {interval_minutes}-minute history query "
                f"unavailable or unauthorized: {error}"
            ) from error
        rows = _kline_rows(raw, code)
        now = _local_time(self._clock())
        bars: list[MinuteBar] = []
        previous_time: datetime | None = None
        seen: set[datetime] = set()
        for row in rows:
            try:
                timestamp = _vendor_time(row["kline_time"])
            except (KeyError, TypeError) as error:
                raise ValueError("invalid bar timestamp") from error
            if (
                timestamp.second
                or timestamp.microsecond
                or timestamp.minute % interval_minutes
            ):
                raise ValueError(
                    "bar timestamp does not align to "
                    f"{interval_minutes} minutes: {timestamp}"
                )
            if not start_date <= timestamp.date() <= end_date:
                raise ValueError(f"bar timestamp outside requested dates: {timestamp}")
            if timestamp in seen:
                raise ValueError(f"duplicate bar timestamp: {timestamp}")
            if previous_time is not None and timestamp < previous_time:
                raise ValueError("bar timestamps must be ordered ascending")
            seen.add(timestamp)
            previous_time = timestamp
            opening = _required_decimal(row, "open")
            high = _required_decimal(row, "high")
            low = _required_decimal(row, "low")
            close = _required_decimal(row, "close")
            volume = _required_int(row, "volume")
            if (
                min(opening, high, low, close) <= 0
                or low > min(opening, close)
                or high < max(opening, close)
                or low > high
            ):
                raise ValueError("invalid OHLC values")
            if volume < 0:
                raise ValueError("bar volume cannot be negative")
            amount = _optional_decimal(row.get("amount"))
            if amount is not None and amount < 0:
                raise ValueError("bar amount cannot be negative")
            bars.append(
                MinuteBar(
                    symbol=code,
                    timestamp=timestamp,
                    interval_minutes=interval_minutes,
                    open=opening,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    amount=amount,
                    is_completed=timestamp + timedelta(minutes=interval_minutes) <= now,
                    source="AmazingData",
                )
            )
        return tuple(bars)

    def get_daily_history(
        self, symbol: str, start_date: date, end_date: date
    ) -> tuple[DailyBar, ...]:
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date")
        code = normalize_symbol(symbol)
        try:
            from AmazingData.query_api.market_data import Period  # type: ignore[import-untyped]

            period = int(Period.day.value)
            raw = self._market_data.query_kline(
                [_vendor_symbol(code)],
                int(start_date.strftime("%Y%m%d")),
                int(end_date.strftime("%Y%m%d")),
                period,
            )
        except Exception as error:
            raise CapabilityUnavailableError(
                f"AmazingData daily-history query unavailable or unauthorized: {error}"
            ) from error
        bars: list[DailyBar] = []
        seen: set[date] = set()
        previous: date | None = None
        for row in _kline_rows(raw, code):
            try:
                timestamp = _vendor_time(row["kline_time"])
            except (KeyError, TypeError) as error:
                raise ValueError("invalid daily-bar timestamp") from error
            day = timestamp.date()
            if day < start_date or day > end_date:
                raise ValueError(f"daily bar outside requested dates: {day}")
            if day in seen or (previous is not None and day < previous):
                raise ValueError("daily bars must be unique and ordered ascending")
            opening = _required_decimal(row, "open")
            high = _required_decimal(row, "high")
            low = _required_decimal(row, "low")
            close = _required_decimal(row, "close")
            volume = _required_int(row, "volume")
            if (
                min(opening, high, low, close) <= 0
                or low > min(opening, close)
                or high < max(opening, close)
                or low > high
            ):
                raise ValueError("invalid daily OHLC values")
            if volume < 0:
                raise ValueError("daily volume cannot be negative")
            amount = _optional_decimal(row.get("amount"))
            if amount is not None and amount < 0:
                raise ValueError("daily amount cannot be negative")
            bars.append(
                DailyBar(
                    code,
                    timestamp,
                    opening,
                    high,
                    low,
                    close,
                    volume,
                    amount,
                    "AmazingData",
                )
            )
            seen.add(day)
            previous = day
        return tuple(bars)

    def get_trades(self, symbol: str) -> tuple[Trade, ...]:
        normalize_symbol(symbol)
        raise CapabilityUnavailableError(
            "AmazingData trade-query capability is unavailable"
        )


def _make_market_data() -> Any:
    from AmazingData.query_api.base_data import BaseData  # type: ignore[import-untyped]
    from AmazingData.query_api.market_data import MarketData

    calendar = BaseData().get_calendar(market="SH")
    return MarketData(calendar)


def _vendor_symbol(symbol: str) -> str:
    if symbol.startswith(("5", "6")):
        exchange = "SH"
    elif symbol.startswith(("0", "2", "3")):
        exchange = "SZ"
    elif symbol.startswith(("4", "8")):
        exchange = "BJ"
    else:
        raise ValueError(f"cannot determine exchange for symbol: {symbol}")
    return f"{symbol}.{exchange}"


def _period_value(interval_minutes: int) -> int:
    from AmazingData.query_api.market_data import Period

    return int(Period.min1.value if interval_minutes == 1 else Period.min5.value)


def _kline_rows(raw: Any, symbol: str) -> list[Mapping[str, Any]]:
    if not raw:
        return []
    if not isinstance(raw, Mapping):
        return _records(raw)
    if "kline_time" in raw:
        return [raw]
    frame = next(
        (
            value
            for key, value in raw.items()
            if normalize_symbol(str(key).split(".")[0]) == symbol
        ),
        None,
    )
    return _records(frame)


def _snapshot_rows(
    raw: Any, symbol: str, session_date: int
) -> list[Mapping[str, Any]]:
    if not isinstance(raw, Mapping):
        return []
    dated = raw.get(str(session_date), raw.get(session_date))
    if not isinstance(dated, Mapping):
        return []
    frame = next(
        (
            value
            for key, value in dated.items()
            if normalize_symbol(str(key).split(".")[0]) == symbol
        ),
        None,
    )
    return _records(frame)


def _required_decimal(row: Mapping[str, Any], key: str) -> Decimal:
    try:
        value = _optional_decimal(row[key])
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"invalid bar {key}: {row.get(key)!r}") from error
    if value is None or not value.is_finite():
        raise ValueError(f"invalid bar {key}: {row.get(key)!r}")
    return value


def _required_int(row: Mapping[str, Any], key: str) -> int:
    value = _required_decimal(row, key)
    if value != value.to_integral_value():
        raise ValueError(f"invalid bar {key}: {row.get(key)!r}")
    return int(value)


def _records(frame: Any) -> list[Mapping[str, Any]]:
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        try:
            values = frame.to_dict(orient="records")
        except TypeError:
            values = frame.to_dict("records")
        return list(values)
    if isinstance(frame, Mapping):
        return [frame]
    return list(frame)


def _vendor_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _local_time(value)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), SHANGHAI)
    if isinstance(value, str) and value.isdigit():
        patterns = {
            12: "%Y%m%d%H%M",
            14: "%Y%m%d%H%M%S",
            17: "%Y%m%d%H%M%S%f",
            20: "%Y%m%d%H%M%S%f",
        }
        pattern = patterns.get(len(value))
        if pattern is not None:
            try:
                return datetime.strptime(value, pattern).replace(tzinfo=SHANGHAI)
            except ValueError:
                pass
    raise ValueError(f"invalid AmazingData timestamp: {value!r}")


def _local_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI)


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        if value != value:
            return None
    except TypeError, ValueError:
        pass
    return Decimal(str(value))


def _optional_int(value: Any) -> int | None:
    number = _optional_decimal(value)
    return None if number is None else int(number)


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)
