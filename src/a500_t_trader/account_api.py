"""Uvicorn entry point for the authenticated account API."""

from __future__ import annotations

import os

import uvicorn

from .account_snapshot_api import create_app
from .cli.runtime import create_provider_from_env
from .market_data import MarketDataService

if os.environ.get("ACCOUNT_API_MARKET_DATA") == "disabled":
    app = create_app()
else:
    app = create_app(market_data=MarketDataService(create_provider_from_env()))


def main() -> None:
    uvicorn.run("a500_t_trader.account_api:app", host="0.0.0.0", port=8000)
