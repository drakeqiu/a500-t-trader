import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal

import ledger


SHADOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_anchor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash_cents INTEGER NOT NULL,
    reset_at TEXT NOT NULL,
    event_cutoff INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_holdings (
    symbol TEXT PRIMARY KEY,
    quantity INTEGER NOT NULL CHECK (quantity >= 0)
);
CREATE TABLE IF NOT EXISTS shadow_events (
    event_id TEXT PRIMARY KEY REFERENCES account_events(event_id)
);
"""


def validate_symbol(symbol, label):
    if not symbol or not symbol.isalnum():
        raise ValueError(f"{label} must be alphanumeric")
    if symbol != "563360":
        raise ValueError("only supports security 563360")

def ensure_shadow(db):
    db.executescript(SHADOW_SCHEMA)


def shadow_project(db):
    state = db.execute("SELECT cash_cents, event_cutoff FROM shadow_anchor WHERE id = 1").fetchone()
    if state is None:
        raise ValueError("confirm a shadow reset first")
    cash, cutoff = state
    holdings = dict(db.execute("SELECT symbol, quantity FROM shadow_holdings"))
    events = db.execute(
        """SELECT e.event_type, e.direction, e.amount_cents, e.symbol, e.quantity
           FROM account_events AS e JOIN shadow_events AS s ON s.event_id = e.event_id
           WHERE e.rowid > ? ORDER BY e.occurred_at, e.event_id""",
        (cutoff,),
    )
    for kind, direction, amount, symbol, qty in events:
        if kind == "cash_flow":
            cash += amount if direction == "in" else -amount
            if cash < 0:
                raise ValueError("insufficient shadow cash; confirmed reset required")
        elif direction == "in":
            holdings[symbol] = holdings.get(symbol, 0) + qty
        else:
            held = holdings.get(symbol, 0)
            if held < qty:
                raise ValueError(f"cannot mirror {symbol} holding adjustment; confirmed reset required")
            holdings[symbol] = held - qty
    return cash, holdings


def reset(args, db):
    if not args.confirm_reset:
        raise ValueError("reset requires --confirm-reset")
    real_cash, real_holdings = ledger.project(db)
    at = ledger.timestamp(args.at) if args.at else datetime.now(timezone.utc).isoformat()
    latest_fact = db.execute(
        "SELECT MAX(occurred_at) FROM (SELECT effective_at AS occurred_at FROM anchor UNION ALL SELECT occurred_at FROM fills WHERE applied = 1 UNION ALL SELECT occurred_at FROM account_events)"
    ).fetchone()[0]
    if latest_fact and at < latest_fact:
        raise ValueError("reset timestamp cannot predate confirmed account facts")
    cutoff = db.execute("SELECT COALESCE(MAX(rowid), 0) FROM account_events").fetchone()[0]
    with db:
        db.execute("INSERT INTO shadow_anchor VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET cash_cents=excluded.cash_cents, reset_at=excluded.reset_at, event_cutoff=excluded.event_cutoff", (real_cash, at, cutoff))
        db.execute("DELETE FROM shadow_holdings")
        db.executemany("INSERT INTO shadow_holdings VALUES (?, ?)", sorted(real_holdings.items()))
    print(f"shadow reset confirmed at {at}; baseline copied from real account")


def record_event(args, db, kind):
    event_id = args.id.strip()
    if not event_id:
        raise ValueError("event id is required")
    at = ledger.timestamp(args.at)
    anchor = db.execute("SELECT effective_at FROM anchor WHERE id = 1").fetchone()
    if anchor is None:
        raise ValueError("confirm a real account anchor first")
    if at < anchor[0]:
        raise ValueError("event cannot predate the real account anchor")
    shadow_anchor = db.execute("SELECT reset_at FROM shadow_anchor WHERE id = 1").fetchone()
    if shadow_anchor and at <= shadow_anchor[0]:
        raise ValueError("event predates the shadow baseline; reconcile and confirm a new reset")
    if kind == "cash_flow":
        amount = ledger.cents(args.amount, "amount")
        symbol = None
        qty = None
    else:
        amount = None
        symbol = args.symbol
        validate_symbol(symbol, "symbol")
        qty = ledger.quantity(args.quantity, "quantity")
    recorded = ledger.timestamp(args.recorded_at) if args.recorded_at else ledger.now()
    if not args.confirm_adjustment:
        raise ValueError("account adjustment requires --confirm-adjustment")
    facts = (at, kind, args.direction, amount, symbol, qty)
    with db:
        previous = db.execute(
            "SELECT occurred_at, event_type, direction, amount_cents, symbol, quantity FROM account_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if previous is not None:
            if previous != facts:
                raise ValueError("event id already exists with different facts")
            print(f"duplicate {kind} {event_id}; no balance change")
            return
        db.execute("INSERT INTO account_events (event_id, occurred_at, event_type, direction, amount_cents, symbol, quantity, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (event_id, *facts, recorded))
        ledger.project(db)
        shadow = db.execute("SELECT 1 FROM shadow_anchor WHERE id = 1").fetchone()
        if shadow:
            db.execute("INSERT INTO shadow_events VALUES (?)", (event_id,))
            shadow_project(db)
    print(f"recorded confirmed {kind} {event_id}; mirrored to shadow" if shadow else
          f"recorded confirmed {kind} {event_id}; run reset to establish shadow baseline")


def compare(args, db):
    real_cash, real_holdings = ledger.project(db)
    shadow_cash, shadow_holdings = shadow_project(db)
    prices = {}
    for item in args.price:
        try:
            symbol, value = item.split("=", 1)
        except ValueError:
            raise ValueError("price must use SYMBOL=PRICE")
        validate_symbol(symbol, "price symbol")
        prices[symbol] = ledger.cents(value, "price")
    symbols = {symbol for symbol, qty in real_holdings.items() if qty} | {
        symbol for symbol, qty in shadow_holdings.items() if qty
    }
    missing = symbols - prices.keys()
    if missing:
        raise ValueError("missing prices for: " + ", ".join(sorted(missing)))
    real_equity = real_cash + sum(qty * prices[symbol] for symbol, qty in real_holdings.items())
    shadow_equity = shadow_cash + sum(qty * prices[symbol] for symbol, qty in shadow_holdings.items())
    excess = real_equity - shadow_equity
    print(f"real equity: {real_equity // 100}.{real_equity % 100:02d}")
    print(f"shadow equity: {shadow_equity // 100}.{shadow_equity % 100:02d}")
    print(f"net excess: {Decimal(excess) / 100:.2f}")


def main():
    parser = argparse.ArgumentParser(description="OFFLINE shadow comparison only; not for trading until broker rules, SDK integration, and live testing are complete")
    parser.add_argument("--db", required=True, help="SQLite account ledger")
    commands = parser.add_subparsers(dest="command", required=True)
    reset_parser = commands.add_parser("reset", help="copy the confirmed real account into shadow")
    reset_parser.add_argument("--confirm-reset", action="store_true", help="explicitly confirm replacing the shadow baseline")
    reset_parser.add_argument("--at", help="reset timestamp (ISO-8601 with timezone)")
    cash = commands.add_parser("cash-flow", help="record and mirror an external cash flow")
    cash.add_argument("--id", required=True)
    cash.add_argument("--at", required=True)
    cash.add_argument("--direction", required=True, choices=("in", "out"))
    cash.add_argument("--amount", required=True)
    cash.add_argument("--recorded-at")
    cash.add_argument("--confirm-adjustment", action="store_true")
    holding = commands.add_parser("holding-adjustment", help="record and mirror an external ETF holding transfer")
    holding.add_argument("--id", required=True)
    holding.add_argument("--at", required=True)
    holding.add_argument("--symbol", required=True)
    holding.add_argument("--direction", required=True, choices=("in", "out"))
    holding.add_argument("--quantity", required=True)
    holding.add_argument("--recorded-at")
    holding.add_argument("--confirm-adjustment", action="store_true")
    compare_parser = commands.add_parser("compare", help="compare account equity at supplied market prices")
    compare_parser.add_argument("--price", action="append", default=[], metavar="SYMBOL=PRICE")
    args = parser.parse_args()
    try:
        db = ledger.connect(args.db)
        try:
            ensure_shadow(db)
            if args.command == "reset":
                reset(args, db)
            elif args.command == "cash-flow":
                record_event(args, db, "cash_flow")
            elif args.command == "holding-adjustment":
                record_event(args, db, "holding_adjustment")
            else:
                compare(args, db)
        finally:
            db.close()
    except (ValueError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
