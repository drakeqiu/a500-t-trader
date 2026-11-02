"""Durable V1 strategy constraints and explicitly linked actual-fill state."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StrategyState:
    """Persisted per-symbol, per-session grid and constraint counters."""

    symbol: str
    session_date: date
    grid_version: int
    grid_layer: int
    grid_round: int
    spacing: Decimal
    same_direction_bars: int
    cooldown_until: datetime | None
    reset_anchor_crossings: int
    daily_trade_count: int
    daily_add_count: int
    daily_turnover: Decimal


@dataclass(frozen=True, slots=True)
class InitialHoldings:
    """An explicit aggregate account holding, not strategy trade history."""

    symbol: str
    as_of: datetime
    protected_floor: int
    cost_basis: Decimal
    total_quantity: int
    sellable_quantity: int


@dataclass(frozen=True, slots=True)
class StrategyRecommendation:
    recommendation_id: str
    symbol: str
    side: str
    created_at: datetime
    grid_version: int

@dataclass(frozen=True, slots=True)
class StrategyFill:
    fill_id: str
    recommendation_id: str | None
    trade_pair_id: str
    side: str
    quantity: int
    price: Decimal
    timestamp: datetime


class IntradayStrategyStateStore:
    """Persist advisory strategy state without treating it as account truth."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS intraday_strategy_state (
                    symbol TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    grid_version INTEGER NOT NULL,
                    grid_layer INTEGER NOT NULL,
                    grid_round INTEGER NOT NULL,
                    spacing TEXT NOT NULL,
                    same_direction_bars INTEGER NOT NULL,
                    cooldown_until TEXT,
                    reset_anchor_crossings INTEGER NOT NULL,
                    daily_trade_count INTEGER NOT NULL,
                    daily_add_count INTEGER NOT NULL,
                    daily_turnover TEXT NOT NULL,
                    PRIMARY KEY(symbol, session_date)
                );
                CREATE TABLE IF NOT EXISTS strategy_initial_holdings (
                    symbol TEXT PRIMARY KEY,
                    record_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_recommendations (
                    recommendation_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
                    created_at TEXT NOT NULL,
                    grid_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_trade_pairs (
                    trade_pair_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    grid_version INTEGER NOT NULL,
                    buy_fill_id TEXT,
                    sell_fill_id TEXT,
                    buy_quantity INTEGER NOT NULL,
                    sell_quantity INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('open','closed'))
                );
                CREATE TABLE IF NOT EXISTS strategy_actual_fills (
                    fill_id TEXT PRIMARY KEY,
                    recommendation_id TEXT NOT NULL,
                    trade_pair_id TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    price TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY(recommendation_id)
                        REFERENCES strategy_recommendations(recommendation_id),
                    FOREIGN KEY(trade_pair_id)
                        REFERENCES strategy_trade_pairs(trade_pair_id)
                );
                CREATE TABLE IF NOT EXISTS consumed_grid_slots (
                    symbol TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    grid_version INTEGER NOT NULL,
                    grid_layer INTEGER NOT NULL,
                    grid_round INTEGER NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
                    PRIMARY KEY(symbol,session_date,grid_version,grid_layer,grid_round,side)
                );
                """
            )

    def save_state(self, state: StrategyState) -> None:
        _validate_state(state)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """INSERT INTO intraday_strategy_state VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,session_date) DO UPDATE SET
                    grid_version=excluded.grid_version,
                    grid_layer=excluded.grid_layer,
                    grid_round=excluded.grid_round,
                    spacing=excluded.spacing,
                    same_direction_bars=excluded.same_direction_bars,
                    cooldown_until=excluded.cooldown_until,
                    reset_anchor_crossings=excluded.reset_anchor_crossings,
                    daily_trade_count=excluded.daily_trade_count,
                    daily_add_count=excluded.daily_add_count,
                    daily_turnover=excluded.daily_turnover""",
                (
                    state.symbol,
                    state.session_date.isoformat(),
                    state.grid_version,
                    state.grid_layer,
                    state.grid_round,
                    str(state.spacing),
                    state.same_direction_bars,
                    _timestamp(state.cooldown_until) if state.cooldown_until else None,
                    state.reset_anchor_crossings,
                    state.daily_trade_count,
                    state.daily_add_count,
                    str(state.daily_turnover),
                ),
            )

    def state(self, symbol: str, session_date: date) -> StrategyState | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT * FROM intraday_strategy_state WHERE symbol=? AND session_date=?",
                (symbol, session_date.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return StrategyState(
            row[0], date.fromisoformat(row[1]), row[2], row[3], row[4],
            Decimal(row[5]), row[6], datetime.fromisoformat(row[7]) if row[7] else None,
            row[8], row[9], row[10], Decimal(row[11]),
        )

    def record_initial_holdings(self, holdings: InitialHoldings) -> None:
        _validate_holdings(holdings)
        payload = {
            "symbol": holdings.symbol,
            "as_of": _timestamp(holdings.as_of),
            "protected_floor": holdings.protected_floor,
            "cost_basis": str(holdings.cost_basis),
            "total_quantity": holdings.total_quantity,
            "sellable_quantity": holdings.sellable_quantity,
        }
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO strategy_initial_holdings VALUES(?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET record_json=excluded.record_json",
                (holdings.symbol, json.dumps(payload, separators=(",", ":"))),
            )

    def initial_holdings(self, symbol: str) -> InitialHoldings | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT record_json FROM strategy_initial_holdings WHERE symbol=?",
                (symbol,),
            ).fetchone()
        if row is None:
            return None
        values = json.loads(row[0])
        return InitialHoldings(
            values["symbol"], datetime.fromisoformat(values["as_of"]),
            values["protected_floor"], Decimal(values["cost_basis"]),
            values["total_quantity"], values["sellable_quantity"],
        )

    def record_recommendation(self, recommendation: StrategyRecommendation) -> None:
        _validate_side(recommendation.side)
        _validate_timestamp(recommendation.created_at, "recommendation timestamp")
        if (not recommendation.recommendation_id.strip() or not recommendation.symbol.strip()
                or recommendation.grid_version < 1):
            raise ValueError("recommendation id, symbol and positive grid version are required")
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO strategy_recommendations VALUES(?,?,?,?,?)",
                (recommendation.recommendation_id, recommendation.symbol,
                 recommendation.side, _timestamp(recommendation.created_at),
                 recommendation.grid_version),
            )

    def consume_grid_slot(
        self, symbol: str, session_date: date, grid_version: int,
        grid_layer: int, grid_round: int, side: str,
    ) -> bool:
        """Persist a once-per-layer/round use; return false if already consumed."""
        _validate_side(side)
        if not symbol.strip() or grid_version < 1 or grid_layer < 0 or grid_round < 0:
            raise ValueError("symbol and non-negative grid coordinates are required")
        with sqlite3.connect(self.path) as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO consumed_grid_slots VALUES(?,?,?,?,?,?)",
                (symbol, session_date.isoformat(), grid_version, grid_layer, grid_round, side),
            )
            return cursor.rowcount == 1

    def confirm_fill(
        self, fill: StrategyFill, *, confirmed: bool, simulated: bool
    ) -> bool:
        """Record only confirmed, non-simulated fills linked to a matching recommendation."""
        if not confirmed or simulated or fill.recommendation_id is None:
            return False
        _validate_side(fill.side)
        _validate_timestamp(fill.timestamp, "fill timestamp")
        if (not fill.fill_id.strip() or not fill.trade_pair_id.strip()
                or fill.quantity <= 0 or not fill.price.is_finite() or fill.price <= 0):
            raise ValueError("fill id, pair id, positive quantity and price are required")
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA foreign_keys=ON")
            recommendation = db.execute(
                "SELECT symbol,side,grid_version FROM strategy_recommendations "
                "WHERE recommendation_id=?",
                (fill.recommendation_id,),
            ).fetchone()
            if recommendation is None:
                return False
            symbol, side, recommendation_grid_version = recommendation
            if side != fill.side:
                raise ValueError("fill side must match its recommendation")
            pair = db.execute(
                "SELECT symbol,grid_version,buy_fill_id,sell_fill_id,buy_quantity,sell_quantity "
                "FROM strategy_trade_pairs WHERE trade_pair_id=?",
                (fill.trade_pair_id,),
            ).fetchone()
            if pair is None:
                if fill.side != "buy":
                    raise ValueError("a strategy trade pair must begin with a linked buy fill")
                db.execute(
                    "INSERT INTO strategy_trade_pairs VALUES(?,?,?,?,NULL,?,0,'open')",
                    (fill.trade_pair_id, symbol, recommendation_grid_version,
                     fill.fill_id, fill.quantity),
                )
            else:
                pair_symbol, _, _, sell_fill, buy_qty, sell_qty = pair
                if pair_symbol != symbol:
                    raise ValueError("trade pair symbol must match the recommendation")
                if fill.side == "buy":
                    if sell_fill is not None:
                        raise ValueError("cannot add a buy fill after the pair has begun closing")
                    db.execute(
                        "UPDATE strategy_trade_pairs SET buy_quantity=buy_quantity+? "
                        "WHERE trade_pair_id=?", (fill.quantity, fill.trade_pair_id)
                    )
                else:
                    if buy_qty < sell_qty + fill.quantity:
                        raise ValueError("sell fill exceeds linked strategy buy quantity")
                    db.execute(
                        "UPDATE strategy_trade_pairs SET sell_fill_id=COALESCE(sell_fill_id,?),"
                        "sell_quantity=sell_quantity+?,status=CASE WHEN buy_quantity=sell_quantity+? "
                        "THEN 'closed' ELSE 'open' END WHERE trade_pair_id=?",
                        (fill.fill_id, fill.quantity, fill.quantity, fill.trade_pair_id),
                    )
            db.execute(
                "INSERT INTO strategy_actual_fills VALUES(?,?,?,?,?,?,?)",
                (fill.fill_id, fill.recommendation_id, fill.trade_pair_id, fill.side,
                 fill.quantity, str(fill.price), _timestamp(fill.timestamp)),
            )
        return True

    def trade_pairs(self) -> list[dict[str, object]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT trade_pair_id,symbol,grid_version,buy_fill_id,sell_fill_id,status "
                "FROM strategy_trade_pairs ORDER BY trade_pair_id"
            )
            return [dict(row) for row in rows]


def _validate_state(state: StrategyState) -> None:
    if not state.symbol.strip() or state.grid_version < 1 or state.grid_layer < 0 or state.grid_round < 0:
        raise ValueError("symbol and non-negative grid coordinates are required")
    if state.spacing <= 0 or not state.spacing.is_finite() or state.daily_turnover < 0:
        raise ValueError("spacing must be positive and turnover non-negative")
    counters = (state.same_direction_bars, state.reset_anchor_crossings,
                state.daily_trade_count, state.daily_add_count)
    if any(value < 0 for value in counters):
        raise ValueError("strategy counters must be non-negative")
    if state.cooldown_until is not None:
        _validate_timestamp(state.cooldown_until, "cooldown timestamp")


def _validate_holdings(holdings: InitialHoldings) -> None:
    _validate_timestamp(holdings.as_of, "holdings timestamp")
    if (not holdings.symbol.strip() or holdings.protected_floor < 0
            or holdings.total_quantity < holdings.protected_floor
            or holdings.sellable_quantity < 0
            or holdings.sellable_quantity > holdings.total_quantity
            or not holdings.cost_basis.is_finite() or holdings.cost_basis < 0):
        raise ValueError("initial holding quantities and cost basis are inconsistent")


def _validate_side(side: str) -> None:
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")


def _validate_timestamp(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")


def _timestamp(value: datetime) -> str:
    _validate_timestamp(value, "timestamp")
    return value.isoformat()
