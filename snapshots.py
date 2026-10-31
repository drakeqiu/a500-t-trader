import argparse
from datetime import date
import json
import sqlite3
import sys

import ledger
import orders


SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    business_at TEXT NOT NULL,
    cash_cents INTEGER NOT NULL,
    holdings TEXT NOT NULL,
    fills_through TEXT,
    orders_scope_known INTEGER NOT NULL CHECK (orders_scope_known IN (0, 1)),
    covered_orders TEXT NOT NULL,
    frozen_cash_cents INTEGER NOT NULL,
    frozen_holdings TEXT NOT NULL,
    reconciliation TEXT NOT NULL CHECK (reconciliation IN ('MATCH', 'MISMATCH'))
);
CREATE TABLE IF NOT EXISTS risk_stop_days (
    trade_date TEXT PRIMARY KEY,
    triggering_pnl_cents INTEGER NOT NULL,
    triggered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def pairs(values, parser, name):
    result = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"{name} must be SYMBOL=VALUE")
        key, raw = value.split("=", 1)
        if not key or key in result:
            raise ValueError(f"{name} symbols must be nonempty and unique")
        result[key] = parser(raw, name)
    return result


def latest(db):
    row = db.execute("SELECT snapshot_id, business_at, cash_cents, holdings, fills_through, orders_scope_known, covered_orders, frozen_cash_cents, frozen_holdings, reconciliation FROM broker_snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
    if row is None:
        raise ValueError("capture a broker snapshot first")
    return row


def capture(args, db):
    holdings = pairs(args.holding, lambda value, name: ledger.quantity(value, name, allow_zero=True), "holding")
    frozen_holdings = pairs(args.frozen_holding, lambda value, name: ledger.quantity(value, name, allow_zero=True), "frozen holding")
    covered = sorted(set(args.covered_order))
    if len(covered) != len(args.covered_order):
        raise ValueError("covered order IDs must be unique")
    snapshot_id = args.id.strip()
    if not snapshot_id:
        raise ValueError("snapshot ID is required")
    at = ledger.timestamp(args.at)
    fills_through = ledger.timestamp(args.fills_through) if args.fills_through else None
    if fills_through and fills_through > at:
        raise ValueError("fill coverage cannot extend beyond snapshot business time")
    ledger_cash, ledger_holdings = ledger.project(db, through=at)
    reconciliation = "MATCH" if ledger_cash == ledger.cents(args.cash, "cash", allow_zero=True) and ledger_holdings == {k: v for k, v in holdings.items() if v} else "MISMATCH"
    db.execute("INSERT INTO broker_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (snapshot_id, at, ledger.cents(args.cash, "cash", allow_zero=True), json.dumps(holdings, sort_keys=True),
                fills_through, int(args.orders_scope_known), json.dumps(covered), ledger.cents(args.frozen_cash, "frozen cash", allow_zero=True),
                json.dumps(frozen_holdings, sort_keys=True), reconciliation))
    db.commit()
    print(f"snapshot {snapshot_id} captured at {at}")
    print(f"reconciliation: {reconciliation}")


def status(args, db):
    row = latest(db)
    snapshot_id, at, _, _, fills_through, orders_known, raw_covered, frozen_cash, raw_frozen_holdings, reconciliation = row
    covered = json.loads(raw_covered)
    frozen_holdings = json.loads(raw_frozen_holdings)
    fills_known = fills_through is not None
    blocked = reconciliation != "MATCH" or not fills_known or not orders_known
    print(f"snapshot: {snapshot_id} at {at}")
    print("covered fields: cash, holdings")
    print(f"reconciliation: {reconciliation if not blocked else 'BLOCKED' if reconciliation == 'MATCH' else reconciliation}")
    print(f"fills coverage: {'THROUGH ' + fills_through if fills_known else 'UNKNOWN'}")
    print(f"orders coverage: {'KNOWN' if orders_known else 'UNKNOWN'}")
    print(f"covered order IDs: {', '.join(covered) if covered else 'none'}")
    print(f"broker frozen cash: {frozen_cash // 100}.{frozen_cash % 100:02d}")
    print("broker frozen holdings: " + (", ".join(f"{symbol}={quantity}" for symbol, quantity in sorted(frozen_holdings.items())) or "none"))
    print(f"openings: {'BLOCKED' if blocked else 'ALLOWED'}")


def resource_projection(db):
    row = latest(db)
    snapshot_id, at, cash, raw_holdings, fills_through, orders_known, raw_covered, frozen_cash, raw_frozen_holdings, reconciliation = row
    holdings = json.loads(raw_holdings)
    frozen_holdings = json.loads(raw_frozen_holdings)
    covered = set(json.loads(raw_covered))
    blocked = reconciliation != "MATCH" or fills_through is None or not orders_known
    if fills_through:
        fills = db.execute("SELECT occurred_at, symbol, side, quantity, price_cents FROM fills WHERE applied=1 AND occurred_at > ? AND occurred_at <= ? ORDER BY occurred_at, fill_id", (fills_through, at)).fetchall()
        for _, symbol, side, quantity, price in fills:
            if side == "buy":
                cash -= quantity * price
                holdings[symbol] = holdings.get(symbol, 0) + quantity
            else:
                cash += quantity * price
                holdings[symbol] = holdings.get(symbol, 0) - quantity
        fills = db.execute("SELECT occurred_at, symbol, side, quantity, price_cents FROM fills WHERE applied=1 AND occurred_at > ? ORDER BY occurred_at, fill_id", (at,)).fetchall()
        for _, symbol, side, quantity, price in fills:
            if side == "buy":
                cash -= quantity * price
                holdings[symbol] = holdings.get(symbol, 0) + quantity
            else:
                cash += quantity * price
                holdings[symbol] = holdings.get(symbol, 0) - quantity
    total_cash = cash
    total_holdings = holdings.copy()
    cash -= frozen_cash
    for symbol, quantity in frozen_holdings.items():
        holdings[symbol] = holdings.get(symbol, 0) - quantity
    reserved_cash = 0
    reserved_holdings = {}
    if orders_known:
        for order_id, symbol, side, quantity, price in db.execute("SELECT order_id, symbol, side, remaining_quantity, price_cents FROM orders WHERE status IN ('open', 'partial')"):
            if order_id in covered:
                continue
            if side == "buy":
                reserved_cash += quantity * price
            else:
                reserved_holdings[symbol] = reserved_holdings.get(symbol, 0) + quantity
    cash -= reserved_cash
    for symbol, quantity in reserved_holdings.items():
        holdings[symbol] = holdings.get(symbol, 0) - quantity
    if min([cash, *holdings.values()]) < 0:
        blocked = True
    return (snapshot_id, at, reconciliation, blocked, total_cash, cash, total_holdings,
            frozen_cash, frozen_holdings, reserved_cash, reserved_holdings, holdings)


def resources(args, db):
    (snapshot_id, at, reconciliation, blocked, total_cash, cash, total_holdings,
     frozen_cash, frozen_holdings, reserved_cash, reserved_holdings, holdings) = resource_projection(db)
    display_cash = f"{'-' if cash < 0 else ''}{abs(cash) // 100}.{abs(cash) % 100:02d}"
    print(f"snapshot: {snapshot_id} at {at}")
    print(f"reconciliation: {reconciliation if reconciliation != 'MATCH' else 'BLOCKED' if blocked else 'MATCH'}")
    display_total_cash = f"{total_cash // 100}.{total_cash % 100:02d}"
    print(f"projected total cash: {display_total_cash}")
    print(f"available cash: {display_cash}")
    print("holdings:")
    for symbol, quantity in sorted(total_holdings.items()):
        print(f"{symbol}: {quantity}")
    print(f"broker frozen cash: {frozen_cash // 100}.{frozen_cash % 100:02d}")
    print("broker frozen holdings:")
    for symbol, quantity in sorted(frozen_holdings.items()):
        print(f"{symbol}: {quantity}")
    print("active order reserved cash: " + f"{reserved_cash // 100}.{reserved_cash % 100:02d}")
    print("active order reserved holdings:")
    for symbol, quantity in sorted(reserved_holdings.items()):
        print(f"{symbol}: {quantity}")
    print("available holdings:")
    for symbol, quantity in sorted(holdings.items()):
        print(f"{symbol}: {quantity}")
    for symbol in sorted(set(total_holdings) | set(holdings)):
        print(f"{symbol} sellable quantity: UNAVAILABLE; T+1 restricted quantity: UNAVAILABLE")
    print("sellability evidence: unavailable; no confirmed broker sellable snapshot or verified exchange calendar")
    print(f"openings: {'BLOCKED' if blocked else 'ALLOWED'}")

def risk(args, db):
    candidate_risk = ledger.cents(args.candidate_risk, "candidate risk", allow_zero=True)
    negative = args.daily_new_pnl.startswith("-")
    daily_pnl = ledger.cents(args.daily_new_pnl[1:] if negative else args.daily_new_pnl,
                             "daily new-entry P&L", allow_zero=True) * (-1 if negative else 1)
    trade_date = date.today().isoformat()
    if daily_pnl <= -30000:
        with db:
            db.execute("INSERT OR IGNORE INTO risk_stop_days (trade_date, triggering_pnl_cents) VALUES (?, ?)",
                       (trade_date, daily_pnl))
    stop = db.execute("SELECT triggering_pnl_cents FROM risk_stop_days WHERE trade_date = ?", (trade_date,)).fetchone()
    blocked = []
    if stop is not None:
        blocked.append(f"daily stop remains locked after reaching {'-' if stop[0] < 0 else ''}{abs(stop[0]) // 100}.{abs(stop[0]) % 100:02d} yuan")
    if candidate_risk > 24000:
        blocked.append("candidate risk exceeds 240.00 yuan")
    try:
        if resource_projection(db)[3]:
            blocked.append("resource projection is blocked by reconciliation or unavailable resources")
    except ValueError:
        blocked.append("resource projection unavailable; capture a reconciled snapshot")
    entry_allowed = not blocked
    action_allowed = entry_allowed if args.action == "entry" else True
    print(f"action: {args.action}")
    print(f"candidate risk: {candidate_risk // 100}.{candidate_risk % 100:02d} / 240.00")
    print(f"daily new-entry net P&L: {'-' if daily_pnl < 0 else ''}{abs(daily_pnl) // 100}.{abs(daily_pnl) % 100:02d}; stop at -300.00")
    print(f"new entry: {'ALLOWED' if entry_allowed else 'BLOCKED'}")
    print(f"action status: {'AVAILABLE' if action_allowed else 'BLOCKED'}")
    print("rationale: " + ("; ".join(blocked) if blocked else "candidate within both hard limits and resource projection is reconciled"))
    if args.action != "entry" and not entry_allowed:
        print("risk exit and accounting maintenance remain available while new entries are stopped")



def main():
    parser = argparse.ArgumentParser(description="Record external broker snapshots without changing the real ledger")
    parser.add_argument("--db", required=True, help="SQLite account database")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("capture", help="record a broker observation and its evidence scope")
    create.add_argument("--id", required=True)
    create.add_argument("--at", required=True, help="snapshot business time")
    create.add_argument("--cash", required=True, help="broker total cash, yuan")
    create.add_argument("--holding", action="append", default=[], metavar="SYMBOL=QUANTITY")
    create.add_argument("--fills-through", help="inclusive fill coverage boundary; omission means unknown")
    create.add_argument("--orders-scope-known", action="store_true", help="confirm all broker-covered order IDs are identified")
    decision = commands.add_parser("risk", help="decide whether a new entry is permitted; exits and accounting maintenance remain separate")
    decision.add_argument("--candidate-risk", required=True, help="prospective risk in yuan, from the risk calculation")
    decision.add_argument("--daily-new-pnl", required=True, help="today's new-entry net P&L in yuan")
    decision.add_argument("--action", choices=("entry", "exit", "maintenance"), default="entry")
    create.add_argument("--covered-order", action="append", default=[], help="active order already represented in frozen resources")
    create.add_argument("--frozen-cash", default="0", help="cash frozen by broker, yuan")
    create.add_argument("--frozen-holding", action="append", default=[], metavar="SYMBOL=QUANTITY")
    commands.add_parser("status", help="show reconciliation evidence and opening gate")
    commands.add_parser("resources", help="project resources without double-counting covered activity")
    args = parser.parse_args()
    try:
        db = ledger.connect(args.db)
        try:
            db.executescript(orders.SCHEMA)
            db.executescript(SCHEMA)
            {"capture": capture, "status": status, "resources": resources, "risk": risk}[args.command](args, db)
        finally:
            db.close()
    except (ValueError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
