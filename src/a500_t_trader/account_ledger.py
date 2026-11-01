"""SQLite persistence for user-entered account snapshots and manual fills."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .market_data import normalize_symbol
from .strategy.premarket import AccountSnapshot


class AccountLedger:
    """Store user-reported account records, never broker-verified state."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS account_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    as_of TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    classification TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS manual_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    price TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    classification TEXT NOT NULL
                );
                """
            )

    def record_snapshot(self, snapshot: AccountSnapshot) -> int:
        record = asdict(snapshot)
        with sqlite3.connect(self.path) as db:
            cursor = db.execute(
                "INSERT INTO account_snapshots(as_of,record_json,classification) "
                "VALUES(?,?,?)",
                (
                    snapshot.as_of.isoformat(),
                    json.dumps(record, default=_json_default),
                    "user-entered; not broker-verified",
                ),
            )
            lastrowid = cursor.lastrowid
            if lastrowid is None:
                raise TypeError("SQLite insert did not return a row id")
            return lastrowid

    def record_fill(
        self,
        timestamp: datetime,
        symbol: str,
        side: str,
        quantity: int,
        price: Decimal,
        notes: str = "",
    ) -> int:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("manual fill timestamp must include a timezone")
        if side not in {"buy", "sell"}:
            raise ValueError("manual fill side must be buy or sell")
        if quantity <= 0 or price <= 0:
            raise ValueError("manual fill quantity and price must be positive")
        if not price.is_finite():
            raise ValueError("manual fill price must be finite")
        code = normalize_symbol(symbol)
        with sqlite3.connect(self.path) as db:
            cursor = db.execute(
                "INSERT INTO manual_fills(timestamp,symbol,side,quantity,price,notes,"
                "classification) VALUES(?,?,?,?,?,?,?)",
                (
                    timestamp.isoformat(),
                    code,
                    side,
                    quantity,
                    str(price),
                    notes,
                    "user-entered manual fill; not broker-verified",
                ),
            )
            lastrowid = cursor.lastrowid
            if lastrowid is None:
                raise TypeError("SQLite insert did not return a row id")
            return lastrowid

    def snapshots(self) -> list[dict[str, object]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute("SELECT * FROM account_snapshots ORDER BY id")
            ]

    def fills(self, session_date: str | None = None) -> list[dict[str, object]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            if session_date is None:
                rows = db.execute("SELECT * FROM manual_fills ORDER BY timestamp,id")
            else:
                rows = db.execute(
                    "SELECT * FROM manual_fills WHERE substr(timestamp, 1, 10) = ? "
                    "ORDER BY timestamp,id",
                    (session_date,),
                )
            return [dict(row) for row in rows]


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")
