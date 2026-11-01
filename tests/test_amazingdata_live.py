"""Explicit integration checks against the configured AmazingData provider."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from a500_t_trader.cli.runtime import create_provider_from_env
from a500_t_trader.market_data import MarketDataService

SHANGHAI = ZoneInfo("Asia/Shanghai")

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live_market_data_service() -> MarketDataService:
    """Share one authenticated provider across live market-data checks."""
    return MarketDataService(create_provider_from_env())


def test_live_provider_connects(live_market_data_service: MarketDataService) -> None:
    """Provider construction performs a real login and must not be skipped."""
    assert live_market_data_service is not None


def test_live_snapshot_for_563360(
    live_market_data_service: MarketDataService,
) -> None:
    service = live_market_data_service

    snapshot = service.get_snapshot("563360")

    assert snapshot is not None, "AmazingData returned no snapshot for 563360"
    assert snapshot.symbol == "563360"
    assert snapshot.last_price is not None and snapshot.last_price > 0
    assert snapshot.source == "AmazingData"


def test_live_minute_bars_for_563360(
    live_market_data_service: MarketDataService,
) -> None:
    service = live_market_data_service
    bars = service.get_history_bars(
        "563360", today - timedelta(days=10), today, interval_minutes=1
    )

    assert bars, "AmazingData returned no recent minute bars for 563360"
    assert all(bar.symbol == "563360" for bar in bars)
    assert all(bar.interval_minutes == 1 for bar in bars)
    assert all(bar.source == "AmazingData" for bar in bars)

