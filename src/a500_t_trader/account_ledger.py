"""SQLite persistence for user-reported account and fill evidence."""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from .market_data import normalize_symbol
from .strategy.premarket import AccountSnapshot

GALAXY_SOURCE = "galaxy-securities-csv"
GALAXY_CLASSIFICATION = "user-provided Galaxy Securities file; not system-verified"
GALAXY_HEADER = (
    "成交日期",
    "成交时间",
    "证券代码",
    "证券名称",
    "操作",
    "成交数量",
    "成交均价",
    "成交金额",
    "合同编号",
    "成交编号",
    "手续费",
    "印花税",
    "其他杂费",
    "发生金额",
    "资金余额",
    "交易市场",
    "股东帐户",
    "交收日期",
    "证券中文全称",
    "",
)


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
            _create_imported_fills(db)

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

    def imported_fills(
        self, session_date: str | None = None
    ) -> list[dict[str, object]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            if not _table_exists(db, "imported_fills"):
                return []
            if session_date is None:
                rows = db.execute("SELECT * FROM imported_fills ORDER BY timestamp,id")
            else:
                rows = db.execute(
                    "SELECT * FROM imported_fills WHERE substr(timestamp,1,10)=? "
                    "ORDER BY timestamp,id",
                    (session_date,),
                )
            return [dict(row) for row in rows]


def import_galaxy_fills(
    database: str | Path,
    csv_file: str | Path,
    *,
    confirm: bool = False,
    owner: str | None = None,
) -> dict[str, object]:
    """Validate a Galaxy export and optionally atomically persist its fills."""
    records, errors = _read_galaxy_fills(csv_file)
    return _classify_galaxy_fills(
        database, records, errors, confirm=confirm, owner=owner
    )


def preview_galaxy_fills(
    database: str | Path,
    content: bytes,
    *,
    owner: str,
    max_rows: int = 10_000,
) -> dict[str, object]:
    """Validate uploaded CSV bytes and classify against one authenticated owner."""
    records, errors = _parse_galaxy_fills_bytes(content, max_rows=max_rows)
    result = _classify_galaxy_fills(database, records, errors, owner=owner)
    result["_records"] = records
    return result


def _classify_galaxy_fills(
    database: str | Path,
    records: list[dict[str, object]],
    errors: list[dict[str, object]],
    *,
    confirm: bool = False,
    owner: str | None = None,
) -> dict[str, object]:
    inserted_ids: list[int] = []
    candidates: list[dict[str, object]] = []
    duplicates = 0

    def classify(
        existing: dict[str, dict[str, object]],
    ) -> tuple[list[dict[str, object]], int]:
        seen: dict[str, dict[str, object]] = {}
        new_records: list[dict[str, object]] = []
        repeated = 0
        for record in records:
            trade_number = str(record["trade_number"])
            previous = seen.get(trade_number) or existing.get(trade_number)
            if previous is not None:
                if _same_trade(previous, record):
                    repeated += 1
                else:
                    errors.append(
                        {
                            "row": record["_row"],
                            "field": "成交编号",
                            "reason": (
                                "conflicts with another record using this trade number"
                            ),
                        }
                    )
                continue
            seen[trade_number] = record
            new_records.append(record)
        if errors:
            return [], 0
        return new_records, repeated

    if not errors and confirm:
        with sqlite3.connect(str(database)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            _create_imported_fills(db)
            existing = {
                str(row["trade_number"]): dict(row)
                for row in db.execute(
                    "SELECT * FROM imported_fills WHERE source=? AND owner IS ?",
                    (GALAXY_SOURCE, owner),
                )
            }
            candidates, duplicates = classify(existing)
            for record in candidates:
                cursor = db.execute(
                    """INSERT INTO imported_fills(
                        owner,timestamp,symbol,side,quantity,price,trade_amount,fees,
                        stamp_duty,other_charges,occurrence_amount,source,
                        trade_number,contract_number,classification
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (owner,)
                    + tuple(
                        record[key]
                        for key in (
                            "timestamp",
                            "symbol",
                            "side",
                            "quantity",
                            "price",
                            "trade_amount",
                            "fees",
                            "stamp_duty",
                            "other_charges",
                            "occurrence_amount",
                            "source",
                            "trade_number",
                            "contract_number",
                            "classification",
                        )
                    ),
                )
                if cursor.lastrowid is None:
                    raise TypeError("SQLite insert did not return a row id")
                inserted_ids.append(cursor.lastrowid)
    elif not errors:
        preview_existing: dict[str, dict[str, object]] = {}
        if Path(database).exists():
            with closing(_readonly_database(database)) as db:
                if _table_exists(db, "imported_fills"):
                    preview_existing = {
                        str(row["trade_number"]): dict(row)
                        for row in db.execute(
                            "SELECT * FROM imported_fills "
                            "WHERE source=? AND owner IS ?",
                            (GALAXY_SOURCE, owner),
                        )
                    }
        candidates, duplicates = classify(preview_existing)

    return {
        "valid": len(records),
        "new": len(candidates),
        "duplicates": duplicates,
        "errors": len(errors),
        "error_details": errors,
        "inserted_ids": inserted_ids,
        "confirmed": confirm,
    }


def _read_galaxy_fills(
    csv_file: str | Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    try:
        content = Path(csv_file).read_bytes()
    except OSError:
        return [], [
            {"row": 0, "field": "file", "reason": "cannot read strict GB18030 CSV"}
        ]
    return _parse_galaxy_fills_bytes(content)


def _parse_galaxy_fills_bytes(
    content: bytes, *, max_rows: int | None = None
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    try:
        text = content.decode("gb18030", errors="strict")
    except UnicodeError:
        return [], [
            {"row": 0, "field": "file", "reason": "cannot read strict GB18030 CSV"}
        ]
    rows = csv.reader(text.splitlines(), strict=True)
    try:
        header = next(rows)
    except StopIteration:
        return [], [{"row": 1, "field": "header", "reason": "CSV is empty"}]
    except csv.Error:
        return [], [
            {
                "row": rows.line_num or 1,
                "field": "header",
                "reason": "malformed CSV header",
            }
        ]
    if header != list(GALAXY_HEADER):
        return [], [
            {"row": 1, "field": "header", "reason": "unsupported Galaxy CSV header"}
        ]

    parsed: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    record_count = 0
    try:
        for row_number, row in enumerate(rows, 2):
            if not row or all(not value.strip() for value in row):
                continue
            record_count += 1
            if max_rows is not None and record_count > max_rows:
                return [], [
                    {
                        "row": row_number,
                        "field": "limit",
                        "reason": "CSV exceeds 10,000 row limit",
                    }
                ]
            if len(row) != len(GALAXY_HEADER) or row[-1] != "":
                errors.append(
                    {
                        "row": row_number,
                        "field": "row",
                        "reason": "unexpected column structure",
                    }
                )
                continue
            try:
                parsed.append(_galaxy_record(row, row_number))
            except ValueError as error:
                field, reason = str(error).split(":", 1)
                errors.append({"row": row_number, "field": field, "reason": reason})
    except csv.Error:
        errors.append(
            {
                "row": rows.line_num + 1,
                "field": "row",
                "reason": "malformed CSV record",
            }
        )
    return parsed, errors


def _galaxy_record(row: list[str], row_number: int) -> dict[str, object]:
    def fail(field: str, reason: str) -> None:
        raise ValueError(f"{field}:{reason}")

    def decimal(index: int, field: str, *, positive: bool = False) -> Decimal:
        try:
            value = Decimal(row[index].strip())
        except InvalidOperation:
            fail(field, "must be a decimal number")
        if (
            not value.is_finite()
            or (positive and value <= 0)
            or (not positive and field != "发生金额" and value < 0)
        ):
            fail(
                field,
                "must be finite and " + ("positive" if positive else "non-negative"),
            )
        return value

    trade_date, trade_time = row[0].strip(), row[1].strip()
    try:
        if (
            len(trade_date) != 8
            or not trade_date.isascii()
            or not trade_date.isdecimal()
            or len(trade_time) != 8
            or not trade_time.isascii()
            or trade_time[2] != ":"
            or trade_time[5] != ":"
        ):
            raise ValueError
        timestamp = datetime.strptime(
            f"{trade_date} {trade_time}", "%Y%m%d %H:%M:%S"
        ).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    except ValueError:
        fail("成交日期/成交时间", "must be a valid YYYYMMDD date and HH:MM:SS time")
    side = {"买入": "buy", "卖出": "sell"}.get(row[4].strip())
    if side is None:
        fail("操作", "only 买入 and 卖出 are supported")
    quantity_text = row[5].strip()
    try:
        quantity = int(quantity_text)
    except ValueError:
        fail("成交数量", "must be a positive integer")
    if quantity <= 0 or not quantity_text.isdecimal():
        fail("成交数量", "must be a positive integer")
    try:
        symbol = normalize_symbol(row[2].strip())
    except ValueError:
        fail("证券代码", "must be a valid six-digit security code")
    trade_number = row[9].strip()
    if not trade_number:
        fail("成交编号", "is required")
    trade_amount = decimal(7, "成交金额", positive=True)
    price = decimal(6, "成交均价", positive=True)
    fees = decimal(10, "手续费")
    stamp_duty = decimal(11, "印花税")
    other_charges = decimal(12, "其他杂费")
    occurrence_amount = decimal(13, "发生金额")
    return {
        "timestamp": timestamp.isoformat(),
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": str(price),
        "trade_amount": str(trade_amount),
        "fees": str(fees),
        "stamp_duty": str(stamp_duty),
        "other_charges": str(other_charges),
        "occurrence_amount": str(occurrence_amount),
        "source": GALAXY_SOURCE,
        "trade_number": trade_number,
        "contract_number": row[8].strip(),
        "classification": GALAXY_CLASSIFICATION,
        "_row": row_number,
    }


def _same_trade(left: dict[str, object], right: dict[str, object]) -> bool:
    decimal_fields = (
        "price",
        "trade_amount",
        "fees",
        "stamp_duty",
        "other_charges",
        "occurrence_amount",
    )
    exact_fields = (
        "timestamp",
        "symbol",
        "side",
        "quantity",
        "contract_number",
    )
    return all(str(left[key]) == str(right[key]) for key in exact_fields) and all(
        Decimal(str(left[key])) == Decimal(str(right[key])) for key in decimal_fields
    )


def _create_imported_fills(db: sqlite3.Connection) -> None:
    if not _table_exists(db, "imported_fills"):
        _create_imported_fills_table(db)
    else:
        columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(imported_fills)")
        }
        if "owner" not in columns:
            db.execute("ALTER TABLE imported_fills RENAME TO imported_fills_legacy")
            _create_imported_fills_table(db)
            db.execute(
                """INSERT INTO imported_fills(
                    id,owner,timestamp,symbol,side,quantity,price,trade_amount,fees,
                    stamp_duty,other_charges,occurrence_amount,source,trade_number,
                    contract_number,classification
                ) SELECT id,NULL,timestamp,symbol,side,quantity,price,trade_amount,fees,
                    stamp_duty,other_charges,occurrence_amount,source,trade_number,
                    contract_number,classification FROM imported_fills_legacy"""
            )
            db.execute("DROP TABLE imported_fills_legacy")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS imported_fills_owner_trade "
        "ON imported_fills(COALESCE(owner,''),source,trade_number)"
    )


def _create_imported_fills_table(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE imported_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            price TEXT NOT NULL,
            trade_amount TEXT NOT NULL,
            fees TEXT NOT NULL,
            stamp_duty TEXT NOT NULL,
            other_charges TEXT NOT NULL,
            occurrence_amount TEXT NOT NULL,
            source TEXT NOT NULL,
            trade_number TEXT NOT NULL,
            contract_number TEXT NOT NULL,
            classification TEXT NOT NULL,
            UNIQUE(owner, source, trade_number)
        )"""
    )


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _readonly_database(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")
