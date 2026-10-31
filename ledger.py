import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


SCHEMA = """
CREATE TABLE IF NOT EXISTS anchor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    effective_at TEXT NOT NULL,
    cash_cents INTEGER NOT NULL,
    included_through TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holdings (
    symbol TEXT PRIMARY KEY,
    quantity INTEGER NOT NULL CHECK (quantity >= 0)
);
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price_cents INTEGER NOT NULL CHECK (price_cents > 0),
    applied INTEGER NOT NULL CHECK (applied IN (0, 1))
);
"""


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValueError("timestamp must be ISO-8601 with a timezone")
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def cents(value, name, allow_zero=False):
    try:
        amount = Decimal(value)
        scaled = amount * 100
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a positive amount with at most 2 decimals")
    if not amount.is_finite() or scaled != scaled.to_integral_value() or amount < (0 if allow_zero else 1):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a {qualifier} amount with at most 2 decimals")
    return int(scaled)


def quantity(value, name, allow_zero=False):
    try:
        parsed = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be {'a non-negative' if allow_zero else 'a positive'} whole number")
    if str(parsed) != value or parsed < (0 if allow_zero else 1):
        raise ValueError(f"{name} must be {'a non-negative' if allow_zero else 'a positive'} whole number")
    return parsed


def connect(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def parse_holding(value):
    try:
        symbol, amount = value.split("=", 1)
    except ValueError:
        raise ValueError("holding must use SYMBOL=QUANTITY")
    if not symbol or not symbol.isalnum():
        raise ValueError("holding symbol must be alphanumeric")
    return symbol, quantity(amount, "holding quantity", allow_zero=True)


def anchor(args, db):
    effective = timestamp(args.effective_at)
    included = timestamp(args.included_through)
    if included > effective:
        raise ValueError("included-through cannot be later than anchor effective time")
    cash = cents(args.cash, "cash", allow_zero=True)
    holdings = [parse_holding(item) for item in args.holding]
    if len({symbol for symbol, _ in holdings}) != len(holdings):
        raise ValueError("each holding symbol may appear only once")
    with db:
        if db.execute("SELECT 1 FROM anchor").fetchone():
            raise ValueError("an anchor already exists")
        db.execute("INSERT INTO anchor VALUES (1, ?, ?, ?)", (effective, cash, included))
        db.executemany("INSERT INTO holdings VALUES (?, ?)", holdings)
    print(f"anchor confirmed effective {effective}; included-through {included}")


def fill(args, db):
    fill_id = args.id.strip()
    if not fill_id:
        raise ValueError("fill id is required")
    occurred = timestamp(args.at)
    symbol = args.symbol
    if not symbol or not symbol.isalnum():
        raise ValueError("symbol must be alphanumeric")
    if args.side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    qty = quantity(args.quantity, "quantity")
    price = cents(args.price, "price")
    with db:
        anchor_row = db.execute("SELECT effective_at, included_through FROM anchor WHERE id = 1").fetchone()
        if anchor_row is None:
            raise ValueError("confirm an anchor before recording fills")
        effective, included_through = anchor_row
        if occurred < effective and occurred > included_through:
            raise ValueError("fill predates anchor but is outside its included historical scope")
        apply_fill = occurred > included_through
        previous = db.execute("SELECT occurred_at, symbol, side, quantity, price_cents, applied FROM fills WHERE fill_id = ?", (fill_id,)).fetchone()
        current = (occurred, symbol, args.side, qty, price, int(apply_fill))
        if previous is not None:
            if previous != current:
                raise ValueError("fill id already exists with different facts")
            print(f"duplicate fill {fill_id}; no balance change")
            return
        if apply_fill:
            cash = db.execute("SELECT cash_cents FROM anchor WHERE id = 1").fetchone()[0]
            holding = db.execute("SELECT quantity FROM holdings WHERE symbol = ?", (symbol,)).fetchone()
            held = holding[0] if holding else 0
            total = qty * price
            if args.side == "buy":
                if cash < total:
                    raise ValueError("insufficient cash")
                db.execute("UPDATE anchor SET cash_cents = cash_cents - ? WHERE id = 1", (total,))
                db.execute("INSERT INTO holdings VALUES (?, ?) ON CONFLICT(symbol) DO UPDATE SET quantity = quantity + excluded.quantity", (symbol, qty))
            else:
                if held < qty:
                    raise ValueError("insufficient holdings")
                db.execute("UPDATE anchor SET cash_cents = cash_cents + ? WHERE id = 1", (total,))
                db.execute("UPDATE holdings SET quantity = ? WHERE symbol = ?", (held - qty, symbol))
        db.execute("INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?)", (fill_id, occurred, symbol, args.side, qty, price, int(apply_fill)))
    print(f"recorded fill {fill_id}" + ("" if apply_fill else " (included by anchor; not applied)"))


def balance(args, db):
    anchor_row = db.execute("SELECT effective_at, cash_cents, included_through FROM anchor WHERE id = 1").fetchone()
    if anchor_row is None:
        raise ValueError("no confirmed anchor")
    effective, cash, included = anchor_row
    print(f"anchor {effective}; included-through {included}")
    print(f"cash: {cash // 100}.{cash % 100:02d}")
    print("holdings:")
    for symbol, amount in db.execute("SELECT symbol, quantity FROM holdings ORDER BY symbol"):
        print(f"{symbol}: {amount}")
    print("fills:")
    for row in db.execute("SELECT fill_id, occurred_at, symbol, side, quantity, price_cents, applied FROM fills ORDER BY occurred_at, fill_id"):
        fill_id, occurred_at, symbol, side, qty, price, applied = row
        print(f"{fill_id} {occurred_at} {symbol} {side} {qty} @ {Decimal(price) / 100} ({'applied' if applied else 'included by anchor'})")


def main():
    parser = argparse.ArgumentParser(description="Local confirmed-account fill ledger")
    parser.add_argument("--db", required=True, help="SQLite ledger file")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("anchor", help="confirm the initial account anchor")
    init.add_argument("--effective-at", required=True)
    init.add_argument("--cash", required=True)
    init.add_argument("--holding", action="append", default=[], metavar="SYMBOL=QUANTITY")
    init.add_argument("--included-through", required=True, help="inclusive historical-fill time already reflected in the anchor")
    record = commands.add_parser("fill", help="record a broker-confirmed fill")
    record.add_argument("--id", required=True, help="stable broker fill identity")
    record.add_argument("--at", required=True, help="fill business time")
    record.add_argument("--symbol", required=True)
    record.add_argument("--side", required=True, choices=("buy", "sell"))
    record.add_argument("--quantity", required=True)
    record.add_argument("--price", required=True)
    commands.add_parser("balance", help="show balances and their ledger facts")
    args = parser.parse_args()
    try:
        db = connect(args.db)
        try:
            {"anchor": anchor, "fill": fill, "balance": balance}[args.command](args, db)
        finally:
            db.close()
    except (ValueError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
