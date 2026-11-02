"""Shared types and serialization helpers for CLI command modules."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from ..backtest import BacktestMarketData
from ..market_data import DailyBar
from ..strategy.intraday import MonitorMarketData
from ..strategy.postmarket import ReviewMarketData
from ..strategy.premarket import PlanMarketData


class DailyHistoryMarketData(Protocol):
    def get_daily_history(
        self, symbol: str, start_date: date, end_date: date
    ) -> tuple[DailyBar, ...]: ...


MarketData = (
    PlanMarketData
    | BacktestMarketData
    | ReviewMarketData
    | MonitorMarketData
    | DailyHistoryMarketData
)


def json_default(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")
