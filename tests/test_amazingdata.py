from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd  # type: ignore[import-untyped]
import pytest

from a500_t_trader.market_data import CapabilityUnavailableError, MarketDataService
from a500_t_trader.market_data.providers.amazingdata import AmazingDataProvider

SHANGHAI = ZoneInfo("Asia/Shanghai")


class VendorAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.kline_rows: list[dict[str, object]] = []

    def query_snapshot(
        self, codes: list[str], start: int, end: int
    ) -> dict[str, object]:
        self.calls.append(("snapshot", codes, start, end))
        return {
            "20261102": {
                "563360.SH": pd.DataFrame(
                    [
                        {
                            "trade_time": "20261102093100",
                            "last": 1.2345,
                            "pre_close": 1.2,
                            "open": 1.21,
                            "high": 1.24,
                            "low": 1.2,
                            "volume": 3200,
                            "amount": 3950.4,
                            "ask_price1": 1.235,
                            "ask_volume1": 12,
                            "bid_price1": 1.234,
                            "bid_volume1": 18,
                        }
                    ]
                )
            }
        }

    def query_kline(
        self, codes: list[str], start: int, end: int, period: int
    ) -> dict[str, object]:
        self.calls.append(("kline", codes, start, end, period))
        return {"563360.SH": pd.DataFrame(self.kline_rows)}


class SnapshotAPI:
    MarketType = SimpleNamespace(kSSE=1, kSZSE=2)
    SubscribeDataType = SimpleNamespace(
        kSnapshot=10, k1MinKline=1, kTickExecution=23
    )

    def __init__(self, *, deliver_snapshot: bool = True) -> None:
        self.deliver_snapshot = deliver_snapshot
        self.unsubscribed: list[object] = []

    def SubscribeItem(self) -> SimpleNamespace:
        return SimpleNamespace()

    def Subscribe(self, item: object, spi: object) -> int:
        if self.deliver_snapshot:
            spi.OnMDSnapshot(
                SimpleNamespace(
                    security_code="563360",
                    orig_time="20261102093200",
                    last_price=1.2345,
                    pre_close_price=1.2,
                    open_price=1.21,
                    high_price=1.24,
                    low_price=1.2,
                    total_volume_trade=3200,
                    total_value_trade=3950.4,
                    bid_price=[1.234],
                    bid_volume=[18],
                    offer_price=[1.235],
                    offer_volume=[12],
                )
            )
        return 0

    def UnSubscribe(self, item: object, _spi: object) -> int:
        self.unsubscribed.append(item)
        return 0


def _provider(
    api: VendorAPI,
    now: datetime,
    *,
    tgw_api: SnapshotAPI | None = None,
    snapshot_timeout: float = 5,
) -> AmazingDataProvider:
    return AmazingDataProvider(
        "trader",
        "secret",
        "localhost",
        9876,
        login_fn=lambda *_args, **_kwargs: True,
        market_data=api,
        tgw_api=tgw_api,
        snapshot_timeout=snapshot_timeout,
        clock=lambda: now,
    )


def _row(at: str, close: float) -> dict[str, object]:
    return {
        "kline_time": at,
        "open": close - 0.01,
        "high": close + 0.01,
        "low": close - 0.02,
        "close": close,
        "volume": 100,
        "amount": 123.4,
    }


def test_service_queries_intraday_bars_without_filling_gaps_and_marks_current_bar() -> (
    None
):
    api = VendorAPI()
    api.kline_rows = [_row("202611020930", 1.23), _row("202611020932", 1.25)]
    provider = _provider(api, datetime(2026, 11, 2, 9, 32, 30, tzinfo=SHANGHAI))
    service = MarketDataService(provider)

    bars = service.get_intraday_bars(interval_minutes=1)

    assert api.calls == [("kline", ["563360.SH"], 20261102, 20261102, 10000)]
    assert [bar.timestamp for bar in bars] == [
        datetime(2026, 11, 2, 9, 30, tzinfo=SHANGHAI),
        datetime(2026, 11, 2, 9, 32, tzinfo=SHANGHAI),
    ]
    assert [bar.is_completed for bar in bars] == [True, False]
    assert bars[0].amount == Decimal("123.4")


def test_history_query_maps_supported_period_and_vendor_bars() -> None:
    api = VendorAPI()
    api.kline_rows = [_row("202611020930", 1.23)]
    provider = _provider(api, datetime(2026, 11, 5, 12, tzinfo=SHANGHAI))
    service = MarketDataService(provider)

    bars = service.get_history_bars(
        "563360", date(2026, 11, 2), date(2026, 11, 4), interval_minutes=5
    )

    assert len(bars) == 1
    assert bars[0].timestamp == datetime(2026, 11, 2, 9, 30, tzinfo=SHANGHAI)
    assert bars[0].interval_minutes == 5
    assert bars[0].close == Decimal("1.23")
    assert bars[0].is_completed
    assert api.calls == [("kline", ["563360.SH"], 20261102, 20261104, 10002)]


def test_daily_history_queries_day_period_and_returns_validated_daily_bars() -> None:
    api = VendorAPI()
    api.kline_rows = [
        {
            "kline_time": pd.Timestamp("2026-11-02"),
            "open": 1.20,
            "high": 1.25,
            "low": 1.18,
            "close": 1.23,
            "volume": 1000,
            "amount": 1230.0,
        }
    ]
    service = MarketDataService(
        _provider(api, datetime(2026, 11, 5, 12, tzinfo=SHANGHAI))
    )
    bars = service.get_daily_history("563360", date(2026, 11, 1), date(2026, 11, 3))
    assert len(bars) == 1
    assert bars[0].timestamp == datetime(2026, 11, 2, tzinfo=SHANGHAI)
    assert bars[0].close == Decimal("1.23")
    assert bars[0].amount == Decimal("1230.0")
    assert api.calls == [("kline", ["563360.SH"], 20261101, 20261103, 10008)]


def test_bar_query_rejects_timestamps_misaligned_to_interval() -> None:
    api = VendorAPI()
    api.kline_rows = [_row("202611020931", 1.23)]
    service = MarketDataService(
        _provider(api, datetime(2026, 11, 2, 10, tzinfo=SHANGHAI))
    )

    with pytest.raises(ValueError, match="does not align"):
        service.get_intraday_bars(interval_minutes=5)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([_row("202611020931", 1.23), _row("202611020930", 1.24)], "ordered"),
        ([_row("202611020930", 1.23), _row("202611020930", 1.24)], "duplicate"),
        ([_row("202611020930", 1.23) | {"high": 1.0}], "OHLC"),
    ],
)
def test_bar_query_rejects_unordered_duplicate_and_invalid_vendor_data(
    rows: list[dict[str, object]], message: str
) -> None:
    api = VendorAPI()
    api.kline_rows = rows
    service = MarketDataService(
        _provider(api, datetime(2026, 11, 2, 10, tzinfo=SHANGHAI))
    )

    with pytest.raises(ValueError, match=message):
        service.get_intraday_bars()


def test_empty_query_and_unavailable_capability_are_distinct() -> None:
    api = VendorAPI()
    api.kline_rows = []
    provider = _provider(api, datetime(2026, 11, 2, 10, tzinfo=SHANGHAI))
    assert MarketDataService(provider).get_intraday_bars() == ()

    class Unauthorized(VendorAPI):
        def query_kline(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise RuntimeError("permission denied")

    with pytest.raises(CapabilityUnavailableError, match="permission denied"):
        MarketDataService(
            _provider(Unauthorized(), datetime(2026, 11, 2, 10, tzinfo=SHANGHAI))
        ).get_intraday_bars()


def test_service_maps_live_callback_snapshot_and_unsubscribes() -> None:
    calls: list[tuple[object, ...]] = []
    api = VendorAPI()
    subscription_api = SnapshotAPI()

    def login(
        username: str,
        password: str,
        host: str,
        port: int,
        **kwargs: object,
    ) -> bool:
        calls.append((username, password, host, port, kwargs))
        return True

    now = datetime(2026, 11, 2, 9, 32, tzinfo=SHANGHAI)
    provider = AmazingDataProvider(
        "trader",
        "secret",
        "localhost",
        9876,
        login_fn=login,
        market_data=api,
        tgw_api=subscription_api,
        clock=lambda: now,
    )

    snapshot = MarketDataService(provider).get_snapshot("SH.563360")

    assert calls == [
        ("trader", "secret", "localhost", 9876, {"api_mode": "kInternetMode"})
    ]
    assert snapshot is not None
    assert api.calls == []
    assert len(subscription_api.unsubscribed) == 1
    assert snapshot.symbol == "563360"
    assert snapshot.timestamp == datetime(2026, 11, 2, 9, 32, tzinfo=SHANGHAI)
    assert snapshot.received_at == now
    assert snapshot.last_price == Decimal("1.2345")
    assert snapshot.previous_close == Decimal("1.2")
    assert snapshot.cumulative_volume == 3200
    assert snapshot.cumulative_amount == Decimal("3950.4")
    assert snapshot.ask_price == Decimal("1.235")
    assert snapshot.bid_volume == 18
    assert snapshot.source == "AmazingData"


def test_service_snapshot_timeout_is_explicit_and_unsubscribes() -> None:
    api = VendorAPI()
    subscription_api = SnapshotAPI(deliver_snapshot=False)
    provider = _provider(
        api,
        datetime(2026, 11, 2, 9, 32, tzinfo=SHANGHAI),
        tgw_api=subscription_api,
        snapshot_timeout=0.01,
    )

    with pytest.raises(CapabilityUnavailableError, match="snapshot callback timed out"):
        MarketDataService(provider).get_snapshot()

    assert api.calls == []
    assert len(subscription_api.unsubscribed) == 1


def test_rejected_login_is_not_reported_as_an_empty_query() -> None:
    with pytest.raises(CapabilityUnavailableError, match="login was rejected"):
        AmazingDataProvider(
            "trader",
            "wrong",
            "localhost",
            9876,
            login_fn=lambda *_args, **_kwargs: False,
            market_data=VendorAPI(),
        )
