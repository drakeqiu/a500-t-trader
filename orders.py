import argparse
import sqlite3
import sys
from decimal import Decimal

import ledger


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    remaining_quantity INTEGER NOT NULL CHECK (remaining_quantity >= 0 AND remaining_quantity <= quantity),
    price_cents INTEGER NOT NULL CHECK (price_cents > 0),
    status TEXT NOT NULL CHECK (status IN ('open', 'partial', 'filled', 'cancelled', 'rejected'))
);
"""
ACTIVE = ("open", "partial")


def reservations(db, exclude=None):
    query = "SELECT order_id, symbol, side, remaining_quantity, price_cents FROM orders WHERE status IN ('open', 'partial')"
    rows = db.execute(query).fetchall()
    if exclude is not None:
        rows = [row for row in rows if row[0] != exclude]
    cash = sum(remaining * price for _, _, side, remaining, price in rows if side == "buy")
    holdings = {}
    for _, symbol, side, remaining, _ in rows:
        if side == "sell":
            holdings[symbol] = holdings.get(symbol, 0) + remaining
    return cash, holdings


def validate_available(db, order_id, symbol, side, remaining, price):
    anchor = db.execute("SELECT cash_cents FROM anchor WHERE id = 1").fetchone()
    if anchor is None:
        raise ValueError("confirm an anchor before recording orders")
    cash_reserved, holdings_reserved = reservations(db, order_id)
    if side == "buy" and anchor[0] - cash_reserved < remaining * price:
        raise ValueError("insufficient unreserved cash")
    if side == "sell":
        held = db.execute("SELECT quantity FROM holdings WHERE symbol = ?", (symbol,)).fetchone()
        if (held[0] if held else 0) - holdings_reserved.get(symbol, 0) < remaining:
            raise ValueError("insufficient unreserved holdings")


def record(args, db):
    order_id = args.id.strip()
    if not order_id:
        raise ValueError("order id is required")
    symbol = args.symbol
    if not symbol or not symbol.isalnum():
        raise ValueError("symbol must be alphanumeric")
    remaining = ledger.quantity(args.quantity, "quantity")
    price = ledger.cents(args.price, "price")
    previous = db.execute("SELECT symbol, side, quantity, price_cents FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    facts = (symbol, args.side, remaining, price)
    if previous is not None:
        if previous != facts:
            raise ValueError("order id already exists with different facts")
        print(f"duplicate order {order_id}; no change")
        return
    with db:
        validate_available(db, order_id, symbol, args.side, remaining, price)
        db.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, 'open')",
                   (order_id, symbol, args.side, remaining, remaining, price))
    print(f"recorded order {order_id}")


def update_status(args, db):
    current = db.execute("SELECT symbol, side, remaining_quantity, price_cents, status FROM orders WHERE order_id = ?", (args.id,)).fetchone()
    if current is None:
        raise ValueError(f"unknown order {args.id}")
    symbol, side, previous_remaining, price, previous_status = current
    if previous_status not in ACTIVE:
        if previous_status == args.status and (args.remaining_quantity is None or args.remaining_quantity == previous_remaining):
            print(f"order {args.id} already {previous_status}")
            return
        raise ValueError("terminal order status cannot be changed")
    remaining = args.remaining_quantity
    if args.status == "filled":
        if remaining not in (None, 0):
            raise ValueError("filled order must have zero remaining quantity")
        remaining = 0
    elif remaining is None:
        if args.status == "partial":
            raise ValueError("partial status requires --remaining-quantity")
        remaining = previous_remaining
    if remaining < 0:
        raise ValueError("remaining quantity cannot be negative")
    if remaining > previous_remaining:
        raise ValueError("remaining quantity cannot increase")
    if args.status in ACTIVE and remaining == 0:
        raise ValueError("active order must have positive remaining quantity")
    if args.status in ACTIVE:
        validate_available(db, args.id, symbol, side, remaining, price)
    with db:
        db.execute("UPDATE orders SET status = ?, remaining_quantity = ? WHERE order_id = ?",
                   (args.status, remaining, args.id))
    print(f"order {args.id} status {args.status}; remaining {remaining}")


def cancel(args, db):
    if not args.confirmed:
        raise ValueError("cancellation requires --confirmed")
    with db:
        row = db.execute("SELECT status FROM orders WHERE order_id = ?", (args.id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown order {args.id}")
        if row[0] == "cancelled":
            print(f"order {args.id} already cancelled")
            return
        if row[0] not in ACTIVE:
            raise ValueError("terminal order status cannot be changed")
        db.execute("UPDATE orders SET status = 'cancelled', remaining_quantity = 0 WHERE order_id = ?", (args.id,))
    print(f"order {args.id} cancellation confirmed")


def active(args, db):
    if db.execute("SELECT 1 FROM anchor WHERE id = 1").fetchone() is None:
        raise ValueError("no confirmed anchor")
    orders = db.execute("SELECT order_id, symbol, side, remaining_quantity, price_cents, status FROM orders WHERE status IN ('open', 'partial') ORDER BY order_id").fetchall()
    cash, holdings = reservations(db)
    for order_id, symbol, side, remaining, price, status in orders:
        reservation = f"cash {price * remaining // 100}.{price * remaining % 100:02d}" if side == "buy" else f"holdings {remaining}"
        print(f"{order_id} {status} {symbol} {side} {remaining} @ {Decimal(price) / 100:.2f}; reserves {reservation}")
    print(f"reserved cash: {cash // 100}.{cash % 100:02d}")
    print("reserved holdings:")
    for symbol, quantity in sorted(holdings.items()):
        print(f"{symbol}: {quantity}")


def main():
    parser = argparse.ArgumentParser(description="Record manual broker orders and their resource reservations")
    parser.add_argument("--db", required=True, help="SQLite ledger file")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("record", help="record an order submitted in the broker client")
    create.add_argument("--id", required=True, help="stable broker order identity")
    create.add_argument("--symbol", required=True)
    create.add_argument("--side", required=True, choices=("buy", "sell"))
    create.add_argument("--quantity", required=True)
    create.add_argument("--price", required=True, help="limit price in yuan")
    status = commands.add_parser("status", help="update an order status")
    status.add_argument("--id", required=True)
    status.add_argument("--status", required=True, choices=("open", "partial", "filled", "rejected"))
    status.add_argument("--remaining-quantity", type=int)
    cancel_parser = commands.add_parser("cancel", help="record a broker-confirmed cancellation")
    cancel_parser.add_argument("--id", required=True)
    cancel_parser.add_argument("--confirmed", action="store_true", help="confirm the broker reported cancellation")
    commands.add_parser("active", help="show active orders and reserved account resources")
    args = parser.parse_args()
    try:
        db = ledger.connect(args.db)
        try:
            db.executescript(SCHEMA)
            {"record": record, "status": update_status, "cancel": cancel, "active": active}[args.command](args, db)
        finally:
            db.close()
    except (ValueError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
