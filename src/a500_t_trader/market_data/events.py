"""Versioned JSON contract for normalized push market-data events."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from typing import TypeAlias

from . import MinuteBar, Snapshot, Trade

MarketEvent: TypeAlias = Snapshot | MinuteBar | Trade
SCHEMA_VERSION = 1


def encode_market_event(event: MarketEvent) -> dict[str, object]:
    """Encode a normalized event with explicit event-time and ID quality."""
    if isinstance(event, Snapshot):
        event_type = "snapshot"
        event_time = event.timestamp
    elif isinstance(event, MinuteBar):
        event_type = "minute_bar"
        event_time = event.timestamp
    elif isinstance(event, Trade):
        event_type = "trade"
        event_time = event.timestamp
    else:
        raise TypeError(f"unsupported market event: {type(event).__name__}")
    return {
        "schema_version": SCHEMA_VERSION,
        "event_type": event_type,
        "event_time": event_time.isoformat(),
        "source_event_id": None,
        "source_event_id_quality": "absent",
        "symbol": event.symbol,
        "data": _json_value(asdict(event)),
    }


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
