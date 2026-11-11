"""Uvicorn entry point for the authenticated account API."""

from __future__ import annotations

import os

import uvicorn

from .account_snapshot_api import create_app
from .cli.runtime import create_provider_from_env
from .market_data import MarketDataService

_AD_SETTINGS = ("AD_USERNAME", "AD_PASSWORD", "AD_HOST", "AD_PORT")


def _market_data_from_environment() -> MarketDataService | None:
    if not any(os.environ.get(name) for name in _AD_SETTINGS):
        return None
    return MarketDataService(create_provider_from_env())


app = create_app(market_data=_market_data_from_environment())


def main() -> None:
    uvicorn.run("a500_t_trader.account_api:app", host="0.0.0.0", port=8000)
