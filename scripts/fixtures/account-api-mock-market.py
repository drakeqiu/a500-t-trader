"""Test-only account API entrypoint with an explicitly synthetic market source."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import uvicorn

from a500_t_trader.account_snapshot_api import create_app
from a500_t_trader.market_data import MarketDataService, MarketDataSource, Snapshot


class FixtureQuotes:
    def get_snapshot(self, symbol: str) -> Snapshot:
        now = datetime.now(UTC)
        return Snapshot(
            symbol=symbol,
            timestamp=now,
            received_at=now,
            last_price=Decimal("1.00"),
            source="test-fixture-not-live",
        )


app = create_app(market_data=MarketDataService(cast(MarketDataSource, FixtureQuotes())))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
