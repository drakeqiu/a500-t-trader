import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


SCHEMA = """
CREATE TABLE IF NOT EXISTS anchor (
    id INTEGER PRIMARY KEY CHECK (id = 1), effective_at TEXT NOT NULL,
    cash_cents INTEGER NOT NULL, included_through TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holdings (symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL CHECK (quantity >= 0));
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')), quantity INTEGER NOT NULL CHECK (quantity > 0),
    price_cents INTEGER NOT NULL CHECK (price_cents > 0), applied INTEGER NOT NULL CHECK (applied IN (0, 1)),
    recorded_at TEXT NOT NULL DEFAULT '', effective INTEGER NOT NULL DEFAULT 1,
    supersedes_id TEXT REFERENCES fills(fill_id)
);
CREATE TABLE IF NOT EXISTS account_events (
    event_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('cash_flow', 'holding_adjustment')),
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')), amount_cents INTEGER,
    symbol TEXT, quantity INTEGER, recorded_at TEXT NOT NULL DEFAULT '',
    CHECK ((event_type = 'cash_flow' AND amount_cents IS NOT NULL AND amount_cents > 0 AND symbol IS NULL AND quantity IS NULL)
        OR (event_type = 'holding_adjustment' AND amount_cents IS NULL AND symbol IS NOT NULL AND symbol <> '' AND quantity IS NOT NULL AND quantity > 0))
 );
CREATE TABLE IF NOT EXISTS fill_fees (fill_id TEXT PRIMARY KEY REFERENCES fills(fill_id), fee_cents INTEGER NOT NULL CHECK (fee_cents >= 0));
CREATE TABLE IF NOT EXISTS trade_groups (group_id TEXT PRIMARY KEY, direction TEXT NOT NULL CHECK (direction IN ('positive', 'reverse')));
CREATE TABLE IF NOT EXISTS group_fills (fill_id TEXT PRIMARY KEY REFERENCES fills(fill_id), group_id TEXT NOT NULL REFERENCES trade_groups(group_id));
CREATE TABLE IF NOT EXISTS anchor_history (
    history_id INTEGER PRIMARY KEY, old_effective_at TEXT NOT NULL, old_cash_cents INTEGER NOT NULL,
    old_included_through TEXT NOT NULL, old_holdings TEXT NOT NULL, recorded_at TEXT NOT NULL,
    new_effective_at TEXT NOT NULL, new_cash_cents INTEGER NOT NULL, new_included_through TEXT NOT NULL
);
"""


def fifo_matches(fills):
    """Return FIFO matches and open lots for (side, quantity, payload) fills."""
    lots = []
    matches = []
    for side, quantity, payload in fills:
        remaining = quantity
        while remaining and lots and lots[0][0] != side:
            _, prior_quantity, prior_payload = lots[0]
            matched = min(remaining, prior_quantity)
            matches.append((prior_payload, payload, matched))
            remaining -= matched
            lots[0][1] -= matched
            if lots[0][1] == 0:
                lots.pop(0)
        if remaining:
            lots.append([side, remaining, payload])
    return matches, lots


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValueError("timestamp must be ISO-8601 with a timezone")
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def now():
    return datetime.now(timezone.utc).isoformat()


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
    for table, column, definition in (
        ("fills", "recorded_at", "TEXT NOT NULL DEFAULT ''"),
        ("fills", "effective", "INTEGER NOT NULL DEFAULT 1"),
        ("fills", "supersedes_id", "TEXT"),
        ("account_events", "recorded_at", "TEXT NOT NULL DEFAULT ''"),
    ):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    db.execute("UPDATE fills SET recorded_at = occurred_at WHERE recorded_at = ''")
    db.execute("UPDATE account_events SET recorded_at = occurred_at WHERE recorded_at = ''")
    db.commit()
    return db

def validate_symbol(symbol, name="symbol"):
    if symbol != "563360":
        raise ValueError(f"{name} must be security 563360 (SSE)")


def parse_holding(value):
    try:
        symbol, amount = value.split("=", 1)
    except ValueError:
        raise ValueError("holding must use SYMBOL=QUANTITY")
    validate_symbol(symbol, "holding symbol")
    return symbol, quantity(amount, "holding quantity", allow_zero=True)


def anchor_values(args):
    effective = timestamp(args.effective_at)
    included = timestamp(args.included_through)
    if included > effective:
        raise ValueError("included-through cannot be later than anchor effective time")
    cash = cents(args.cash, "cash", allow_zero=True)
    holdings = [parse_holding(item) for item in args.holding]
    if len({symbol for symbol, _ in holdings}) != len(holdings):
        raise ValueError("each holding symbol may appear only once")
    return effective, included, cash, holdings


def anchor(args, db):
    effective, included, cash, holdings = anchor_values(args)
    with db:
        if db.execute("SELECT 1 FROM anchor").fetchone():
            raise ValueError("an anchor already exists")
        db.execute("INSERT INTO anchor VALUES (1, ?, ?, ?)", (effective, cash, included))
        db.executemany("INSERT INTO holdings VALUES (?, ?)", holdings)
    print(f"anchor confirmed effective {effective}; included-through {included}")


def project(db, through=None):
    anchor_row = db.execute("SELECT cash_cents, included_through FROM anchor WHERE id = 1").fetchone()
    if anchor_row is None:
        raise ValueError("confirm a real account anchor first")
    cash, included = anchor_row
    holdings = dict(db.execute("SELECT symbol, quantity FROM holdings"))
    events = []
    events.extend((at, 0, event_id, "fill", (symbol, side, qty, price, fee)) for event_id, at, symbol, side, qty, price, fee in db.execute(
        "SELECT f.fill_id, f.occurred_at, f.symbol, f.side, f.quantity, f.price_cents, COALESCE(x.fee_cents, 0) FROM fills f LEFT JOIN fill_fees x USING (fill_id) WHERE f.applied=1 AND f.effective=1"))
    events.extend((at, 1, event_id, kind, (direction, amount, symbol, qty)) for event_id, at, kind, direction, amount, symbol, qty in db.execute(
        "SELECT event_id, occurred_at, event_type, direction, amount_cents, symbol, quantity FROM account_events"))
    events = [event for event in events if event[0] > included and (through is None or event[0] <= through)]
    for _, _, _, kind, facts in sorted(events):
        if kind == "fill":
            symbol, side, qty, price, fee = facts
            held = holdings.get(symbol, 0)
            cost = qty * price
            if side == "buy":
                if cash < cost + fee:
                    raise ValueError("insufficient cash")
                cash -= cost + fee
                holdings[symbol] = held + qty
            else:
                if held < qty:
                    raise ValueError("insufficient holdings")
                if cash + cost < fee:
                    raise ValueError("insufficient cash for recorded fee")
                cash += cost - fee
                holdings[symbol] = held - qty
        elif kind == "cash_flow":
            direction, amount, _, _ = facts
            if direction == "in":
                cash += amount
            else:
                if cash < amount:
                    raise ValueError("insufficient real cash")
                cash -= amount
        else:
            direction, _, symbol, qty = facts
            held = holdings.get(symbol, 0)
            if direction == "in":
                holdings[symbol] = held + qty
            else:
                if held < qty:
                    raise ValueError(f"insufficient real holdings for {symbol}")
                holdings[symbol] = held - qty
    return cash, holdings


def _fill(args, db, supersedes=None):
    fill_id = args.id.strip()
    if not fill_id:
        raise ValueError("fill id is required")
    occurred = timestamp(args.at)
    recorded = timestamp(args.recorded_at) if args.recorded_at else now()
    symbol = args.symbol
    validate_symbol(symbol)
    if args.side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    qty, price = quantity(args.quantity, "quantity"), cents(args.price, "price")
    with db:
        anchor_row = db.execute("SELECT effective_at, included_through FROM anchor WHERE id = 1").fetchone()
        if anchor_row is None:
            raise ValueError("confirm an anchor before recording fills")
        effective_at, included_through = anchor_row
        if occurred < effective_at and occurred > included_through:
            raise ValueError("fill predates anchor but is outside its included historical scope")
        apply_fill = int(occurred > included_through)
        previous = db.execute("SELECT occurred_at, symbol, side, quantity, price_cents, applied FROM fills WHERE fill_id=?", (fill_id,)).fetchone()
        current = (occurred, symbol, args.side, qty, price, apply_fill)
        if previous is not None:
            if supersedes is None and previous == current:
                print(f"duplicate fill {fill_id}; no balance change")
                return
            raise ValueError("fill id already exists")
        if supersedes:
            old = db.execute("SELECT occurred_at, symbol, side, quantity, price_cents, applied FROM fills WHERE fill_id=? AND effective=1", (supersedes,)).fetchone()
            if old is None:
                raise ValueError("superseded fill must be the current effective version")
            db.execute("UPDATE fills SET effective=0 WHERE fill_id=?", (supersedes,))
        db.execute("INSERT INTO fills (fill_id, occurred_at, symbol, side, quantity, price_cents, applied, recorded_at, effective, supersedes_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                   (fill_id, occurred, symbol, args.side, qty, price, apply_fill, recorded, supersedes))
        if supersedes:
            fee = db.execute("SELECT fee_cents FROM fill_fees WHERE fill_id=?", (supersedes,)).fetchone()
            if fee:
                db.execute("INSERT INTO fill_fees VALUES (?, ?)", (fill_id, fee[0]))
            db.execute("UPDATE group_fills SET fill_id=? WHERE fill_id=?", (fill_id, supersedes))
        project(db)
    print(f"recorded fill {fill_id}" + (f" supersedes {supersedes}" if supersedes else "") + ("" if apply_fill else " (included by anchor; not applied)"))


def fill(args, db):
    _fill(args, db)


def correct(args, db):
    if not args.supersedes.strip():
        raise ValueError("a correction must identify the superseded fill")
    _fill(args, db, args.supersedes.strip())


def money(c):
    return f"{'-' if c < 0 else ''}{abs(c) // 100}.{abs(c) % 100:02d}"


def rebuild(args, db):
    if not args.confirm_rebuild:
        raise ValueError("rebuild requires --confirm-rebuild")
    with db:
        before = project(db)
        included = db.execute("SELECT included_through FROM anchor WHERE id = 1").fetchone()
        if included is None:
            raise ValueError("confirm a real account anchor first")
        db.execute("UPDATE fills SET applied=CASE WHEN effective=1 AND occurred_at>? THEN 1 ELSE 0 END",
                   (included[0],))
        after = project(db)
    print(f"before cash: {money(before[0])}")
    print("before holdings: " + ", ".join(f"{s}={q}" for s, q in sorted(before[1].items())))
    print(f"after cash: {money(after[0])}")
    print("after holdings: " + ", ".join(f"{s}={q}" for s, q in sorted(after[1].items())))


def reanchor(args, db):
    if not args.confirm_reanchor:
        raise ValueError("re-anchor requires --confirm-reanchor")
    effective, included, cash, holdings = anchor_values(args)
    with db:
        previous = db.execute("SELECT effective_at, cash_cents, included_through FROM anchor WHERE id=1").fetchone()
        if previous is None:
            raise ValueError("confirm a real account anchor first")
        old_holdings = dict(db.execute("SELECT symbol, quantity FROM holdings"))
        db.execute("INSERT INTO anchor_history (old_effective_at, old_cash_cents, old_included_through, old_holdings, recorded_at, new_effective_at, new_cash_cents, new_included_through) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (*previous, json.dumps(old_holdings, sort_keys=True), now(), effective, cash, included))
        db.execute("UPDATE anchor SET effective_at=?, cash_cents=?, included_through=? WHERE id=1", (effective, cash, included))
        db.execute("DELETE FROM holdings")
        db.executemany("INSERT INTO holdings VALUES (?, ?)", holdings)
        db.execute("UPDATE fills SET applied=CASE WHEN occurred_at>? THEN 1 ELSE 0 END WHERE effective=1", (included,))
        project(db)
    print(f"re-anchor confirmed effective {effective}; included-through {included}")


def balance(args, db):
    anchor_row = db.execute("SELECT effective_at, cash_cents, included_through FROM anchor WHERE id = 1").fetchone()
    if anchor_row is None:
        raise ValueError("no confirmed anchor")
    effective, _, included = anchor_row
    cash, holdings = project(db)
    print(f"anchor {effective}; included-through {included}")
    for old_effective, old_cash, old_included, old_holdings, recorded, new_effective, new_cash, new_included in db.execute("SELECT old_effective_at, old_cash_cents, old_included_through, old_holdings, recorded_at, new_effective_at, new_cash_cents, new_included_through FROM anchor_history ORDER BY history_id"):
        print(f"re-anchor {old_effective} -> {new_effective} recorded at {recorded}; prior cash {money(old_cash)}, included-through {old_included}; prior holdings {old_holdings}")
    print(f"cash: {money(cash)}")
    print("holdings:")
    for symbol, amount in sorted(holdings.items()):
        print(f"{symbol}: {amount}")
    print("fills:")
    for row in db.execute("SELECT fill_id, occurred_at, symbol, side, quantity, price_cents, applied, recorded_at, effective, supersedes_id FROM fills ORDER BY occurred_at, recorded_at, fill_id"):
        fill_id, at, symbol, side, qty, price, applied, recorded, is_effective, supersedes = row
        status = "effective" if is_effective else "superseded"
        relation = f" supersedes {supersedes}" if supersedes else ""
        print(f"{fill_id} {at} {symbol} {side} {qty} @ {Decimal(price) / 100} ({'applied' if applied else 'included by anchor'}; {status}; recorded at {recorded}{relation})")
    print("confirmed account events:")
    for event_id, at, kind, direction, amount, symbol, qty, recorded in db.execute(
        "SELECT event_id, occurred_at, event_type, direction, amount_cents, symbol, quantity, recorded_at FROM account_events ORDER BY occurred_at, event_id"):
        detail = money(amount) if kind == "cash_flow" else f"{symbol} {qty}"
        print(f"{event_id} {at} {kind} {direction} {detail} (recorded at {recorded})")


def main():
    parser = argparse.ArgumentParser(description="OFFLINE ledger only; not for trading until broker rules, SDK integration, and live testing are complete")
    parser.add_argument("--db", required=True, help="SQLite account database")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("anchor", help="record initial real account anchor")
    create.add_argument("--effective-at", required=True)
    create.add_argument("--cash", required=True)
    create.add_argument("--holding", action="append", default=[])
    create.add_argument("--included-through", required=True)
    reanchor_parser = commands.add_parser("reanchor", help="explicitly replace the accounting anchor")
    reanchor_parser.add_argument("--confirm-reanchor", action="store_true")
    reanchor_parser.add_argument("--effective-at", required=True)
    reanchor_parser.add_argument("--cash", required=True)
    reanchor_parser.add_argument("--holding", action="append", default=[])
    reanchor_parser.add_argument("--included-through", required=True)
    def fill_arguments(command):
        command.add_argument("--id", required=True)
        command.add_argument("--at", required=True)
        command.add_argument("--recorded-at")
        command.add_argument("--symbol", required=True)
        command.add_argument("--side", required=True, choices=("buy", "sell"))
        command.add_argument("--quantity", required=True)
        command.add_argument("--price", required=True)
    fill_arguments(commands.add_parser("fill", help="record a broker-confirmed fill"))
    correction = commands.add_parser("correct", help="append a corrected fill version")
    fill_arguments(correction)
    correction.add_argument("--supersedes", required=True)
    rebuild_parser = commands.add_parser("rebuild", help="compare projected state before and after deterministic replay")
    rebuild_parser.add_argument("--confirm-rebuild", action="store_true")
    commands.add_parser("balance", help="show projected confirmed account")
    args = parser.parse_args()
    try:
        db = connect(args.db)
        try:
            if args.command in ("anchor", "reanchor"):
                (anchor if args.command == "anchor" else reanchor)(args, db)
            elif args.command in ("fill", "correct"):
                (fill if args.command == "fill" else correct)(args, db)
            elif args.command == "rebuild":
                rebuild(args, db)
            else:
                balance(args, db)
        finally:
            db.close()
    except (ValueError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
