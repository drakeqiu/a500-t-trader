"""Explicit market-data source selection and isolated RabbitMQ topology."""

from __future__ import annotations

import os
from enum import StrEnum


class MarketDataMode(StrEnum):
    TGW = "tgw"
    REPLAY = "replay"

    @property
    def exchange(self) -> str:
        return f"market.events.{self.value}"


def market_data_mode(value: str | None = None) -> MarketDataMode:
    configured = value if value is not None else os.environ.get("A500_MARKET_DATA_MODE")
    try:
        return MarketDataMode(configured or "")
    except ValueError as error:
        raise ValueError(
            "A500_MARKET_DATA_MODE must be explicitly set to 'tgw' or 'replay'"
        ) from error


def consumer_queue(purpose: str, mode: MarketDataMode) -> str:
    return f"market.consumer.{purpose}.{mode.value}"
