"""Uvicorn entry point for the authenticated account API."""

from __future__ import annotations

import uvicorn

from .account_snapshot_api import create_app
from .cli.runtime import MissingConfigurationError, create_provider_from_env
from .market_data import MarketDataService, MarketDataSource


def create_market_data_service() -> MarketDataService | None:
    """Create market data when configured; keep API startup optional otherwise."""
    try:
        provider: MarketDataSource = create_provider_from_env()
    except MissingConfigurationError:
        return None
    return MarketDataService(provider)


app = create_app(market_data=create_market_data_service())


def main() -> None:
    uvicorn.run("a500_t_trader.account_api:app", host="0.0.0.0", port=8000)
