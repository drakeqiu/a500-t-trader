"""Uvicorn entry point for the authenticated account API."""

from __future__ import annotations

import os

import uvicorn

from .account_snapshot_api import create_app
from .cli.runtime import MissingConfigurationError, create_provider_from_env
from .market_data import MarketDataService


def create_market_data_service() -> MarketDataService | None:
    """Use configured market data, while keeping it optional for API startup."""
    if os.environ.get("ACCOUNT_API_MARKET_DATA") == "disabled":
        return None
    try:
        return MarketDataService(create_provider_from_env())
    except MissingConfigurationError:
        return None


app = create_app(market_data=create_market_data_service())


def main() -> None:
    uvicorn.run("a500_t_trader.account_api:app", host="0.0.0.0", port=8000)
