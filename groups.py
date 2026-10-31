import argparse
import sqlite3
import sys

from ledger import cents, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_groups (
    group_id TEXT PRIMARY KEY,
    direction TEXT NOT NULL CHECK (direction IN ('positive', 'reverse'))
);
CREATE TABLE IF NOT EXISTS fill_fees (
    fill_id TEXT PRIMARY KEY REFERENCES fills(fill_id),
    fee_cents INTEGER NOT NULL CHECK (fee_cents >= 0)
);
CREATE TABLE IF NOT EXISTS group_fills (
    fill_id TEXT PRIMARY KEY REFERENCES fills(fill_id),
    group_id TEXT NOT NULL REFERENCES trade_groups(group_id)
);
"""


def initialize(db):
    db.executescript(SCHEMA)


def record_fee(args, db):
    fill_id = args.fill_id.strip()
    if not fill_id:
        raise ValueError("fill id is required")
    amount = cents(args.amount, "fee", allow_zero=True)
    with db:
        if db.execute("SELECT 1 FROM fills WHERE fill_id = ?", (fill_id,)).fetchone() is None:
            raise ValueError("fee requires an existing broker-confirmed fill")
        previous = db.execute("SELECT fee_cents FROM fill_fees WHERE fill_id = ?", (fill_id,)).fetchone()
        if previous is not None:
            if previous[0] != amount:
                raise ValueError("fee already recorded for fill with different amount")
            print(f"duplicate fee for fill {fill_id}; no change")
            return
        db.execute("INSERT INTO fill_fees VALUES (?, ?)", (fill_id, amount))
    print(f"recorded fee for fill {fill_id}")


def add_fill(args, db):
    group_id = args.group.strip()
    fill_id = args.fill_id.strip()
    if not group_id or not fill_id:
        raise ValueError("group id and fill id are required")
    with db:
        if db.execute("SELECT 1 FROM fills WHERE fill_id = ?", (fill_id,)).fetchone() is None:
            raise ValueError("group membership requires an existing broker-confirmed fill")
        group = db.execute("SELECT direction FROM trade_groups WHERE group_id = ?", (group_id,)).fetchone()
        if group is not None and group[0] != args.direction:
            raise ValueError("group already exists with different direction")
        existing = db.execute("SELECT group_id FROM group_fills WHERE fill_id = ?", (fill_id,)).fetchone()
        if existing is not None:
            if existing[0] == group_id:
                raise ValueError("fill is already in this group")
            raise ValueError("fill already belongs to another group")
        if group is None:
            db.execute("INSERT INTO trade_groups VALUES (?, ?)", (group_id, args.direction))
        db.execute("INSERT INTO group_fills VALUES (?, ?)", (fill_id, group_id))
    print(f"added fill {fill_id} to group {group_id}")


def money(cents_value):
    sign = "-" if cents_value < 0 else ""
    absolute = abs(cents_value)
    return f"{sign}{absolute // 100}.{absolute % 100:02d}"


def report(args, db):
    group_id = args.group.strip()
    group = db.execute("SELECT direction FROM trade_groups WHERE group_id = ?", (group_id,)).fetchone()
    if group is None:
        raise ValueError(f"unknown group {group_id}")
    direction = group[0]
    rows = db.execute("""
        SELECT f.fill_id, f.occurred_at, f.side, f.quantity, f.price_cents,
               COALESCE(x.fee_cents, 0)
        FROM group_fills g JOIN fills f USING (fill_id)
        LEFT JOIN fill_fees x USING (fill_id)
        WHERE g.group_id = ? AND f.effective=1 ORDER BY f.occurred_at, f.fill_id
    """, (group_id,)).fetchall()
    expected = "sell" if direction == "positive" else "buy"
    if rows and rows[0][2] != expected:
        raise ValueError(f"{direction} group must begin with a {expected} fill")

    lots = []
    matched_by_fill = {}
    gross = 0
    paired_quantity = 0
    for fill_id, occurred, side, quantity, price, fee in rows:
        remaining = quantity
        while remaining and lots and lots[0][1] != side:
            prior = lots[0]
            matched = min(remaining, prior[3])
            gross += matched * (prior[2] - price if prior[1] == "sell" else price - prior[2])
            paired_quantity += matched
            matched_by_fill[prior[0]] = matched_by_fill.get(prior[0], 0) + matched
            matched_by_fill[fill_id] = matched_by_fill.get(fill_id, 0) + matched
            remaining -= matched
            prior[3] -= matched
            if prior[3] == 0:
                lots.pop(0)
        if remaining:
            lots.append([fill_id, side, price, remaining])

    paired_fee = 0
    unpaired_fee = 0
    for fill_id, occurred, side, quantity, price, fee in rows:
        allocated = fee * matched_by_fill.get(fill_id, 0) // quantity
        paired_fee += allocated
        unpaired_fee += fee - allocated
    exposure = {"buy": 0, "sell": 0}
    basis = 0
    for fill_id, side, price, quantity in lots:
        exposure[side] += quantity
        basis += price * quantity

    print(f"group: {group_id}")
    print(f"direction: {direction}")
    print(f"fills: {len(rows)}")
    print(f"paired quantity: {paired_quantity}")
    print(f"paired net P&L: {money(gross - paired_fee)}")
    print(f"unpaired exposure: buy {exposure['buy']}, sell {exposure['sell']}")
    print(f"unpaired basis: {money(basis)}")
    print(f"unpaired fee: {money(unpaired_fee)}")
    for fill_id, occurred, side, quantity, price, fee in rows:
        print(f"fill: {fill_id} {occurred} {side} {quantity} @ {money(price)}; fee {money(fee)}")


def main():
    parser = argparse.ArgumentParser(description="Record fill fees and report auditable T groups")
    parser.add_argument("--db", required=True, help="SQLite ledger file")
    commands = parser.add_subparsers(dest="command", required=True)
    fee = commands.add_parser("fee", help="record actual fee for a broker-confirmed fill")
    fee.add_argument("--fill-id", required=True)
    fee.add_argument("--amount", required=True)
    add = commands.add_parser("add", help="add a broker-confirmed fill to a T group")
    add.add_argument("--group", required=True)
    add.add_argument("--direction", choices=("positive", "reverse"), required=True)
    add.add_argument("--fill-id", required=True)
    show = commands.add_parser("report", help="show paired result and unpaired exposure")
    show.add_argument("--group", required=True)
    args = parser.parse_args()
    try:
        db = connect(args.db)
        try:
            initialize(db)
            {"fee": record_fee, "add": add_fill, "report": report}[args.command](args, db)
        finally:
            db.close()
    except (ValueError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
