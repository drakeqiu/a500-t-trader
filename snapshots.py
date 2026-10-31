import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import sqlite3
import sys
from decimal import Decimal, InvalidOperation

import ledger
import orders

from typing import NamedTuple


class ResourceProjection(NamedTuple):
    snapshot_id: str
    at: str
    reconciliation: str
    blocked: bool
    total_cash: int
    cash: int
    total_holdings: dict
    frozen_cash: int
    frozen_holdings: dict
    reserved_cash: int
    reserved_holdings: dict
    holdings: dict
    sellable_holdings: dict
    t1_restricted_holdings: dict

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
    sellable_holdings TEXT NOT NULL DEFAULT '{}',
    t1_restricted_holdings TEXT NOT NULL DEFAULT '{}',
    reconciliation TEXT NOT NULL CHECK (reconciliation IN ('MATCH', 'MISMATCH'))
);
CREATE TABLE IF NOT EXISTS risk_stop_days (
    trade_date TEXT PRIMARY KEY,
    triggering_pnl_cents INTEGER NOT NULL,
    triggered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def realized_daily_pnl(db):
    rows = db.execute("""
        SELECT f.fill_id, f.occurred_at, f.side, f.quantity, f.price_cents,
               COALESCE(x.fee_cents, 0)
        FROM fills f LEFT JOIN fill_fees x USING (fill_id)
        WHERE f.applied=1 AND f.effective=1 ORDER BY f.occurred_at, f.fill_id
    """).fetchall()
    matches, _ = ledger.fifo_matches((row[2], row[3], row) for row in rows)
    pnl = {}
    for opener, closer, matched in matches:
        _, occurred, _, quantity, price, fee = closer
        day = datetime.fromisoformat(occurred).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        gross = matched * (opener[4] - price if opener[2] == "sell" else price - opener[4])
        opener_fee = opener[5] * matched // opener[3]
        closer_fee = fee * matched // quantity
        pnl[day] = pnl.get(day, 0) + gross - opener_fee - closer_fee
    return pnl


def pairs(values, parser, name):
    result = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"{name} must be SYMBOL=VALUE")
        key, raw = value.split("=", 1)
        if key in result:
            raise ValueError(f"{name} symbols must be unique")
        ledger.validate_symbol(key, name)
        result[key] = parser(raw, name)
    return result

def latest(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(broker_snapshots)")}
    if not columns:
        raise ValueError("capture a broker snapshot first")
    for column in ("sellable_holdings", "t1_restricted_holdings"):
        if column not in columns:
            db.execute(f"ALTER TABLE broker_snapshots ADD COLUMN {column} TEXT NOT NULL DEFAULT '{{}}'")
    row = db.execute("SELECT snapshot_id, business_at, cash_cents, holdings, fills_through, orders_scope_known, covered_orders, frozen_cash_cents, frozen_holdings, reconciliation, sellable_holdings, t1_restricted_holdings FROM broker_snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
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
    ledger_cash, ledger_holdings = ledger.project(db, at)
    broker_cash = ledger.cents(args.cash, "cash", allow_zero=True)
    broker_holdings = {key: value for key, value in holdings.items() if value}
    reconciliation = "MATCH" if ledger_cash == broker_cash and ledger_holdings == broker_holdings else "MISMATCH"
    sellable = pairs(args.sellable_holding, lambda value, name: ledger.quantity(value, name, allow_zero=True), "sellable holding")
    restricted = pairs(args.t1_restricted_holding, lambda value, name: ledger.quantity(value, name, allow_zero=True), "T+1 restricted holding")
    if set(sellable) - set(holdings) or set(restricted) - set(holdings):
        raise ValueError("sellable and T+1 restricted quantities require total holding evidence")
    if any(sellable.get(symbol, 0) + restricted.get(symbol, 0) > quantity
           for symbol, quantity in holdings.items()):
        raise ValueError("sellable and T+1 restricted quantities exceed total holdings")
    db.execute("INSERT INTO broker_snapshots (snapshot_id, business_at, cash_cents, holdings, fills_through, orders_scope_known, covered_orders, frozen_cash_cents, frozen_holdings, reconciliation, sellable_holdings, t1_restricted_holdings) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (snapshot_id, at, broker_cash, json.dumps(holdings, sort_keys=True),
                fills_through, int(args.orders_scope_known), json.dumps(covered), ledger.cents(args.frozen_cash, "frozen cash", allow_zero=True),
                json.dumps(frozen_holdings, sort_keys=True), reconciliation, json.dumps(sellable, sort_keys=True),
                json.dumps(restricted, sort_keys=True)))
    db.commit()
    print(f"snapshot {snapshot_id} captured at {at}")
    print(f"reconciliation: {reconciliation}")
    print(f"cash delta (broker - ledger): {ledger.money(broker_cash - ledger_cash)}")
    for symbol in sorted(set(holdings) | set(ledger_holdings)):
        delta = holdings.get(symbol, 0) - ledger_holdings.get(symbol, 0)
        print(f"{symbol} holdings delta (broker - ledger): {delta}")
    if reconciliation != "MATCH":
        print("explicit reconciliation confirmation required before openings can be allowed")


def status(args, db):
    row = latest(db)
    snapshot_id, at, _, _, fills_through, orders_known, raw_covered, frozen_cash, raw_frozen_holdings, reconciliation, _, _ = row
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
    snapshot_id, at, cash, raw_holdings, fills_through, orders_known, raw_covered, frozen_cash, raw_frozen_holdings, reconciliation, raw_sellable, raw_restricted = row
    holdings = json.loads(raw_holdings)
    sell_proceeds = 0
    frozen_holdings = json.loads(raw_frozen_holdings)
    sellable = json.loads(raw_sellable)
    restricted = json.loads(raw_restricted)
    covered = set(json.loads(raw_covered))
    blocked = (reconciliation != "MATCH" or fills_through is None or not orders_known or
               any(symbol not in sellable or symbol not in restricted for symbol in holdings))
    if fills_through:
        fills = db.execute("SELECT occurred_at, symbol, side, quantity, price_cents, COALESCE(x.fee_cents, 0) FROM fills f LEFT JOIN fill_fees x USING (fill_id) WHERE f.applied=1 AND f.effective=1 AND f.occurred_at > ? AND f.occurred_at <= ? ORDER BY f.occurred_at, f.fill_id", (fills_through, at)).fetchall()
        for _, symbol, side, quantity, price, fee in fills:
            if side == "buy":
                cash -= quantity * price + fee
                holdings[symbol] = holdings.get(symbol, 0) + quantity
                restricted[symbol] = restricted.get(symbol, 0) + quantity
            else:
                proceeds = quantity * price
                cash += proceeds - fee
                sell_proceeds += proceeds
                holdings[symbol] = holdings.get(symbol, 0) - quantity
                if symbol not in sellable or sellable[symbol] < quantity:
                    blocked = True
                    sellable[symbol] = 0
                else:
                    sellable[symbol] -= quantity
        fills = db.execute("SELECT occurred_at, symbol, side, quantity, price_cents, COALESCE(x.fee_cents, 0) FROM fills f LEFT JOIN fill_fees x USING (fill_id) WHERE f.applied=1 AND f.effective=1 AND f.occurred_at > ? ORDER BY f.occurred_at, f.fill_id", (at,)).fetchall()
        for _, symbol, side, quantity, price, fee in fills:
            if side == "buy":
                cash -= quantity * price + fee
                holdings[symbol] = holdings.get(symbol, 0) + quantity
                restricted[symbol] = restricted.get(symbol, 0) + quantity
            else:
                proceeds = quantity * price
                cash += proceeds - fee
                sell_proceeds += proceeds
                holdings[symbol] = holdings.get(symbol, 0) - quantity
                if symbol not in sellable or sellable[symbol] < quantity:
                    blocked = True
                    sellable[symbol] = 0
                else:
                    sellable[symbol] -= quantity
    account_events = db.execute("SELECT event_type, direction, amount_cents, symbol, quantity FROM account_events WHERE occurred_at > ? ORDER BY occurred_at, event_id", (at,)).fetchall()
    for event_type, direction, amount, symbol, quantity in account_events:
        if event_type == "cash_flow":
            cash += amount if direction == "in" else -amount
        elif direction == "in":
            holdings[symbol] = holdings.get(symbol, 0) + quantity
            restricted[symbol] = restricted.get(symbol, 0) + quantity
        else:
            holdings[symbol] = holdings.get(symbol, 0) - quantity
    total_cash = cash
    total_holdings = holdings.copy()
    cash -= sell_proceeds + frozen_cash
    for symbol, quantity in frozen_holdings.items():
        holdings[symbol] = holdings.get(symbol, 0) - quantity
    reserved_cash = 0
    reserved_holdings = {}
    active_orders = db.execute("SELECT order_id, symbol, side, remaining_quantity, price_cents FROM orders WHERE status IN ('open', 'partial')").fetchall()
    active_ids = {row[0] for row in active_orders}
    if not covered.issubset(active_ids):
        blocked = True
    covered_cash = 0
    covered_holdings = {}
    for order_id, symbol, side, quantity, price in active_orders:
        if order_id not in covered:
            continue
        if side == "buy":
            covered_cash += quantity * price
        else:
            covered_holdings[symbol] = covered_holdings.get(symbol, 0) + quantity
    covered_adequate = covered_cash <= frozen_cash and all(
        quantity <= frozen_holdings.get(symbol, 0) for symbol, quantity in covered_holdings.items()
    )
    if not covered_adequate or not covered.issubset(active_ids):
        blocked = True
    if orders_known:
        reserved_cash += max(0, covered_cash - frozen_cash)
        for symbol, quantity in covered_holdings.items():
            reserved_holdings[symbol] = max(0, quantity - frozen_holdings.get(symbol, 0))
        for order_id, symbol, side, quantity, price in active_orders:
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
    available_sellable = dict(sellable)
    for symbol, quantity in frozen_holdings.items():
        available_sellable[symbol] = max(0, available_sellable.get(symbol, 0) - quantity)
    for symbol, quantity in reserved_holdings.items():
        available_sellable[symbol] = max(0, available_sellable.get(symbol, 0) - quantity)
    return ResourceProjection(snapshot_id, at, reconciliation, blocked, total_cash, cash, total_holdings,
                              frozen_cash, frozen_holdings, reserved_cash, reserved_holdings, holdings,
                              available_sellable, restricted)


def resources(args, db):
    projection = resource_projection(db)
    snapshot_id, at, reconciliation = (projection.snapshot_id, projection.at, projection.reconciliation)
    blocked, total_cash, cash = (projection.blocked, projection.total_cash, projection.cash)
    total_holdings, frozen_cash = (projection.total_holdings, projection.frozen_cash)
    frozen_holdings, reserved_cash = (projection.frozen_holdings, projection.reserved_cash)
    reserved_holdings, holdings = (projection.reserved_holdings, projection.holdings)
    sellable, restricted = projection.sellable_holdings, projection.t1_restricted_holdings
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
        if symbol in sellable and symbol in restricted:
            print(f"{symbol} sellable quantity: {sellable[symbol]}; T+1 restricted quantity: {restricted[symbol]}")
        else:
            print(f"{symbol} sellable quantity: UNAVAILABLE; T+1 restricted quantity: UNAVAILABLE")
    print("sellability evidence: broker-confirmed snapshot; T+1 quantities are not inferred from weekday calendars")
    print(f"openings: {'BLOCKED' if blocked else 'ALLOWED'}")

def risk(args, db):
    market = ledger.cents(args.market_price, "market price")
    candidate_quantity = ledger.quantity(args.candidate_quantity, "candidate quantity")
    try:
        fee_rate = Decimal(args.fee_rate)
        stress_rate = Decimal(args.stress_move)
        execution_rate = Decimal(args.execution_buffer)
    except InvalidOperation as error:
        raise ValueError("risk rates must be valid decimal numbers") from error
    if not all(value.is_finite() for value in (fee_rate, stress_rate, execution_rate)):
        raise ValueError("risk rates must be finite numbers")
    if market <= 0 or fee_rate < 0 or stress_rate < Decimal("0.004") or execution_rate < Decimal("0.001"):
        raise ValueError("market price must be positive; fee rate cannot be negative; stress and execution buffer must be at least 0.40% and 0.10%")
    risk_rate = stress_rate + execution_rate + fee_rate * 2
    def conservative_risk(quantity):
        return int((Decimal(quantity * market) * risk_rate).to_integral_value(rounding="ROUND_CEILING"))
    candidate_risk = conservative_risk(candidate_quantity)
    grouped = {}
    existing_risk = 0
    for fill_id, side, quantity, price, fee, group_id in db.execute("""
        SELECT f.fill_id, f.side, f.quantity, f.price_cents, COALESCE(x.fee_cents, 0), g.group_id
        FROM fills f LEFT JOIN fill_fees x USING (fill_id)
        LEFT JOIN group_fills gf USING (fill_id) LEFT JOIN trade_groups g USING (group_id)
        WHERE f.effective=1 AND f.applied=1 ORDER BY f.occurred_at, f.fill_id
    """):
        if group_id is None:
            existing_risk += conservative_risk(quantity) + fee
        else:
            grouped.setdefault(group_id, []).append((side, quantity, price, fee))
    for fills in grouped.values():
        matches, _ = ledger.fifo_matches((side, quantity, (side, price))
                                         for side, quantity, price, fee in fills)
        net_quantity = 0
        fees = 0
        for side, quantity, price, fee in fills:
            fees += fee
            net_quantity += quantity if side == "buy" else -quantity
        realized = sum(matched * (opener_price - closer_price if opener_side == "sell"
                                  else closer_price - opener_price)
                       for (opener_side, opener_price), (_, closer_price), matched in matches)
        existing_risk += conservative_risk(abs(net_quantity)) + fees + max(0, -realized)
    pending_risk = 0
    for quantity, in db.execute("SELECT remaining_quantity FROM orders WHERE status IN ('open','partial')"):
        pending_risk += conservative_risk(quantity)
    candidate_risk += existing_risk + pending_risk
    trade_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    daily_by_day = realized_daily_pnl(db)
    with db:
        for pnl_date, amount in daily_by_day.items():
            if amount <= -30000:
                db.execute("INSERT OR IGNORE INTO risk_stop_days (trade_date, triggering_pnl_cents) VALUES (?, ?)",
                           (pnl_date, amount))
    stop = db.execute("SELECT triggering_pnl_cents FROM risk_stop_days WHERE trade_date = ?", (trade_date,)).fetchone()
    blocked = []
    if stop is not None:
        blocked.append(f"daily stop remains locked after reaching {'-' if stop[0] < 0 else ''}{abs(stop[0]) // 100}.{abs(stop[0]) % 100:02d} yuan")
    if candidate_risk > 24000:
        blocked.append("candidate risk exceeds 240.00 yuan")
    try:
        projection = resource_projection(db)
        if projection.blocked:
            blocked.append("resource projection is blocked by reconciliation or unavailable resources")
        candidate_cash = int((Decimal(candidate_quantity * market) * (1 + fee_rate)).to_integral_value(rounding="ROUND_CEILING"))
        if projection.cash - candidate_cash < 16000000:
            blocked.append("entry would breach the required 160000.00 yuan cash reserve")
    except ValueError:
        blocked.append("resource projection unavailable; capture a reconciled snapshot")
    entry_allowed = not blocked
    action_reasons = []
    if args.action == "entry":
        action_allowed = entry_allowed
    elif args.action == "exit":
        if not args.symbol:
            action_reasons.append("exit requires a symbol to verify existing holdings")
        else:
            try:
                projection = resource_projection(db)
                if projection.blocked:
                    action_reasons.append("resource projection is blocked; broker-confirmed sellable resources are unavailable")
                elif args.symbol not in projection.sellable_holdings:
                    action_reasons.append(f"broker-confirmed sellable quantity for {args.symbol} is unavailable")
                else:
                    available = projection.sellable_holdings[args.symbol]
                    print(f"broker-confirmed sellable quantity: {available}")
                    if available == 0:
                        action_reasons.append(f"no broker-confirmed sellable quantity for {args.symbol}")
                    elif candidate_quantity > available:
                        action_reasons.append(f"exit quantity exceeds broker-confirmed sellable quantity for {args.symbol} ({available})")
            except ValueError:
                action_reasons.append("resource projection is unavailable; capture a reconciled snapshot with sellability evidence")
        action_allowed = not action_reasons
    else:
        action_allowed = True
    print(f"candidate risk: {candidate_risk // 100}.{candidate_risk % 100:02d} / 240.00")
    daily_pnl = daily_by_day.get(trade_date, 0)
    print(f"daily realized P&L: {'-' if daily_pnl < 0 else ''}{abs(daily_pnl) // 100}.{abs(daily_pnl) % 100:02d}; stop at -300.00")
    print(f"new entry: {'ALLOWED' if entry_allowed else 'BLOCKED'}")
    print(f"action status: {'AVAILABLE' if action_allowed else 'BLOCKED'}")
    reasons = blocked if args.action == "entry" else action_reasons
    print("rationale: " + ("; ".join(reasons) if reasons else "candidate within both hard limits and resource projection is reconciled"))
    if args.action != "entry" and not entry_allowed:
        print("risk exit and accounting maintenance remain available while new entries are stopped")



def main():
    parser = argparse.ArgumentParser(description="OFFLINE broker-observation bookkeeping only; not for trading until broker rules, SDK integration, and live testing are complete")
    parser.add_argument("--db", required=True, help="SQLite account database")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("capture", help="record a broker observation and its evidence scope")
    create.add_argument("--id", required=True)
    create.add_argument("--at", required=True, help="snapshot business time")
    create.add_argument("--cash", required=True, help="broker total cash, yuan")
    create.add_argument("--holding", action="append", default=[], metavar="SYMBOL=QUANTITY")
    create.add_argument("--sellable-holding", action="append", default=[], metavar="SYMBOL=QUANTITY", help="broker-confirmed legally sellable quantity")
    create.add_argument("--t1-restricted-holding", action="append", default=[], metavar="SYMBOL=QUANTITY", help="broker-confirmed T+1 restricted quantity")
    create.add_argument("--fills-through", help="inclusive fill coverage boundary; omission means unknown")
    create.add_argument("--orders-scope-known", action="store_true", help="confirm all broker-covered order IDs are identified")
    decision = commands.add_parser("risk", help="decide entry and evidence-backed exit availability; accounting maintenance remains separate")
    decision.add_argument("--market-price", required=True, help="current explicit trusted market price, yuan")
    decision.add_argument("--candidate-quantity", required=True, help="candidate quantity in shares")
    decision.add_argument("--fee-rate", required=True, help="conservative per-leg fee rate")
    decision.add_argument("--stress-move", default="0.004", help="stress rate; minimum 0.40%%")
    decision.add_argument("--execution-buffer", default="0.001", help="execution buffer; minimum 0.10%%")
    decision.add_argument("--action", choices=("entry", "exit", "maintenance"), default="entry")
    decision.add_argument("--symbol", help="existing holding symbol required to verify an exit")
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
