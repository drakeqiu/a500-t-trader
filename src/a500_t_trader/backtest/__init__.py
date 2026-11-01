"""Minute-history backtesting with reproducible local bar snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Protocol

from ..market_data import DailyBar, MinuteBar
from ..strategy.premarket import AccountSnapshot, PlanConfig

HUNDRED = Decimal(100)


class BacktestMarketData(Protocol):
    def get_history_bars(
        self, symbol: str, start_date: date, end_date: date, interval_minutes: int
    ) -> tuple[MinuteBar, ...]: ...


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    symbol: str
    side: str
    quantity: int
    signal_timestamp: datetime
    fill_timestamp: datetime
    fill_price: Decimal
    costs: Decimal
    slippage: Decimal
    status: str = "simulated; not an actual trade"


@dataclass(frozen=True, slots=True)
class BacktestResult:
    status: str
    symbol: str
    start_date: date
    end_date: date
    interval_minutes: int
    strategy_parameters: dict[str, object]
    implementation_version: str
    fill_assumption: str
    cost_bps: Decimal
    slippage_bps: Decimal
    snapshot_id: str
    snapshot_bar_count: int
    simulated_fills: tuple[SimulatedFill, ...]
    initial_cash: Decimal
    initial_holdings: int
    final_cash: Decimal
    final_holdings: int
    strategy_final_value: Decimal
    baseline_final_value: Decimal
    strategy_return: Decimal
    baseline_return: Decimal
    strategy: dict[str, Decimal]
    baseline: dict[str, Decimal]
    initial_account_snapshot: dict[str, object]
    run_id: int = 0
    provenance: str = "minute-history snapshot; simulated fills are not actual trades"


def run_backtest(
    config: PlanConfig,
    account: AccountSnapshot,
    bars: tuple[MinuteBar, ...],
    *,
    start_date: date,
    end_date: date,
    interval_minutes: int,
    cost_bps: Decimal = Decimal(0),
    slippage_bps: Decimal = Decimal(0),
    implementation_version: str = "1",
) -> BacktestResult:
    """Run explicit configured price triggers using next-bar-open fills."""
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")
    if interval_minutes not in (1, 5):
        raise ValueError("interval_minutes must be 1 or 5")
    if cost_bps < 0 or slippage_bps < 0:
        raise ValueError("cost and slippage basis points cannot be negative")
    if not implementation_version.strip():
        raise ValueError("implementation_version must not be empty")
    symbols = {rule.symbol for rule in config.rules}
    if len(symbols) != 1:
        raise ValueError("a backtest requires exactly one configured ETF")
    symbol = next(iter(symbols))
    selected = tuple(
        sorted(
            (
                bar
                for bar in bars
                if start_date <= bar.timestamp.date() <= end_date and bar.is_completed
            ),
            key=lambda bar: bar.timestamp,
        )
    )
    if not selected:
        raise ValueError(
            "no completed minute bars are available for the requested range"
        )
    if any(
        bar.symbol != symbol or bar.interval_minutes != interval_minutes
        for bar in selected
    ):
        raise ValueError(
            "minute history does not match the configured symbol and interval"
        )
    if len({bar.timestamp for bar in selected}) != len(selected):
        raise ValueError("minute history contains duplicate timestamps")
    first_timestamp = selected[0].timestamp
    if account.as_of.tzinfo is None or first_timestamp.tzinfo is None:
        raise ValueError(
            "initial account and minute history timestamps must include time zones"
        )
    if account.as_of > first_timestamp:
        raise ValueError(
            "initial account snapshot must not follow the first history bar"
        )
    rule = config.rules[0]
    initial_cash = account.available_cash
    initial_holdings = account.holdings.get(symbol, 0)
    holdings = initial_holdings
    cash = initial_cash
    immediately_sellable = min(account.sellable.get(symbol, 0), holdings)
    acquired: list[tuple[date, int]] = []
    fills: list[SimulatedFill] = []
    for previous, current in zip(selected, selected[1:], strict=False):
        # T+1: a lot becomes sellable only after a later session date appears.
        newly_sellable = sum(
            qty
            for acquired_date, qty in acquired
            if acquired_date < current.timestamp.date()
        )
        immediately_sellable += newly_sellable
        acquired = [
            (day, qty) for day, qty in acquired if day >= current.timestamp.date()
        ]
        signal: str | None = None
        if rule.buy_below is not None and previous.close <= rule.buy_below:
            signal = "buy"
        elif rule.sell_above is not None and previous.close >= rule.sell_above:
            signal = "sell"
        if signal == "buy":
            market_price = current.open * (Decimal(1) + slippage_bps / Decimal(10000))
            cash_limit = min(cash, rule.max_buy_cash or cash)
            quantity = int(
                (
                    cash_limit
                    / (market_price * (1 + cost_bps / Decimal(10000)))
                    / HUNDRED
                ).to_integral_value(rounding=ROUND_DOWN)
                * 100
            )
            if quantity:
                notional = market_price * quantity
                costs = notional * cost_bps / Decimal(10000)
                slippage = (market_price - current.open) * quantity
                cash -= notional + costs
                holdings += quantity
                acquired.append((current.timestamp.date(), quantity))
                fills.append(
                    SimulatedFill(
                        symbol,
                        "buy",
                        quantity,
                        previous.timestamp,
                        current.timestamp,
                        market_price,
                        costs,
                        slippage,
                    )
                )
        elif signal == "sell":
            quantity = min(
                max(0, holdings - rule.minimum_holding), immediately_sellable
            )
            quantity = quantity // 100 * 100
            if quantity:
                market_price = current.open * (
                    Decimal(1) - slippage_bps / Decimal(10000)
                )
                notional = market_price * quantity
                costs = notional * cost_bps / Decimal(10000)
                slippage = (current.open - market_price) * quantity
                cash += notional - costs
                holdings -= quantity
                immediately_sellable -= quantity
                fills.append(
                    SimulatedFill(
                        symbol,
                        "sell",
                        quantity,
                        previous.timestamp,
                        current.timestamp,
                        market_price,
                        costs,
                        slippage,
                    )
                )
    final_price = selected[-1].close
    strategy_value = cash + holdings * final_price
    baseline_value = initial_cash + initial_holdings * final_price
    initial_value = initial_cash + initial_holdings * selected[0].close
    if initial_value <= 0:
        raise ValueError("initial portfolio value must be positive")
    snapshot_id = snapshot_digest(selected)
    return BacktestResult(
        "completed",
        symbol,
        start_date,
        end_date,
        interval_minutes,
        {
            "buy_below": rule.buy_below,
            "sell_above": rule.sell_above,
            "max_buy_cash": rule.max_buy_cash,
            "minimum_holding": rule.minimum_holding,
        },
        implementation_version,
        "next_bar_open",
        cost_bps,
        slippage_bps,
        snapshot_id,
        len(selected),
        tuple(fills),
        initial_cash,
        initial_holdings,
        cash,
        holdings,
        strategy_value,
        baseline_value,
        (strategy_value - initial_value) / initial_value,
        (baseline_value - initial_value) / initial_value,
        {
            "final_value": strategy_value,
            "return": (strategy_value - initial_value) / initial_value,
        },
        {
            "final_value": baseline_value,
            "return": (baseline_value - initial_value) / initial_value,
        },
        {
            "as_of": account.as_of,
            "holdings": account.holdings,
            "cost_basis": account.cost_basis,
            "sellable": account.sellable,
            "available_cash": account.available_cash,
        },
    )


def snapshot_digest(bars: tuple[MinuteBar, ...]) -> str:
    return hashlib.sha256(_bars_json(bars).encode("utf-8")).hexdigest()


class BacktestRepository:
    """SQLite-backed shared history and immutable snapshots for backtest evidence."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS shared_bars (
                    symbol TEXT NOT NULL, interval_minutes INTEGER NOT NULL,
                    timestamp TEXT NOT NULL, bar_json TEXT NOT NULL,
                    PRIMARY KEY (symbol, interval_minutes, timestamp)
                );
                CREATE TABLE IF NOT EXISTS shared_history_ranges (
                    symbol TEXT NOT NULL, interval_minutes INTEGER NOT NULL,
                    start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                    PRIMARY KEY (symbol, interval_minutes, start_date, end_date)
                );
                CREATE TABLE IF NOT EXISTS minute_snapshots (
                    snapshot_id TEXT PRIMARY KEY, bars_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backtests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL REFERENCES minute_snapshots(snapshot_id),
                    result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shared_daily_bars (
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    bar_json TEXT NOT NULL,
                    PRIMARY KEY (symbol, timestamp)
                );
                CREATE TABLE IF NOT EXISTS shared_daily_history_ranges (
                    symbol TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    PRIMARY KEY (symbol, start_date, end_date)
                );
            """)

    def save(
        self, bars: tuple[MinuteBar, ...], result: BacktestResult
    ) -> BacktestResult:
        content = _bars_json(bars)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest != result.snapshot_id:
            raise ValueError("result snapshot does not match supplied minute bars")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for bar in bars:
                db.execute(
                    "INSERT INTO shared_bars VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(symbol, interval_minutes, timestamp) DO UPDATE SET "
                    "bar_json=excluded.bar_json",
                    (
                        bar.symbol,
                        bar.interval_minutes,
                        bar.timestamp.isoformat(),
                        _bar_json(bar),
                    ),
                )
            db.execute(
                "INSERT OR IGNORE INTO shared_history_ranges VALUES (?, ?, ?, ?)",
                (
                    result.symbol,
                    result.interval_minutes,
                    result.start_date.isoformat(),
                    result.end_date.isoformat(),
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO minute_snapshots VALUES (?, ?)",
                (digest, content),
            )
            cursor = db.execute(
                "INSERT INTO backtests(snapshot_id, result_json) VALUES (?, '')",
                (digest,),
            )
            lastrowid = cursor.lastrowid
            if lastrowid is None:
                raise TypeError("SQLite insert did not return a row id")
            result = replace(result, run_id=lastrowid)
            db.execute(
                "UPDATE backtests SET result_json=? WHERE id=?",
                (_json(asdict(result)), result.run_id),
            )
        return result

    def get_shared_history(
        self, symbol: str, start_date: date, end_date: date, interval_minutes: int
    ) -> tuple[MinuteBar, ...] | None:
        with self._connect() as db:
            covered = db.execute(
                "SELECT 1 FROM shared_history_ranges WHERE symbol=? "
                "AND interval_minutes=? AND start_date=? AND end_date=?",
                (
                    symbol,
                    interval_minutes,
                    start_date.isoformat(),
                    end_date.isoformat(),
                ),
            ).fetchone()
            if covered is None:
                return None
            rows = db.execute(
                "SELECT bar_json FROM shared_bars WHERE symbol=? "
                "AND interval_minutes=? AND timestamp>=? AND timestamp<? "
                "ORDER BY timestamp",
                (
                    symbol,
                    interval_minutes,
                    start_date.isoformat(),
                    datetime.combine(end_date, datetime.max.time()).isoformat(),
                ),
            ).fetchall()
        return tuple(_bar_from_json(row[0]) for row in rows)

    def save_daily_history(
        self, bars: tuple[DailyBar, ...], start_date: date, end_date: date
    ) -> None:
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for bar in bars:
                db.execute(
                    "INSERT INTO shared_daily_bars VALUES (?, ?, ?) "
                    "ON CONFLICT(symbol, timestamp) "
                    "DO UPDATE SET bar_json=excluded.bar_json",
                    (bar.symbol, bar.timestamp.isoformat(), _daily_bar_json(bar)),
                )
            if bars:
                db.execute(
                    "INSERT OR IGNORE INTO shared_daily_history_ranges "
                    "VALUES (?, ?, ?)",
                    (bars[0].symbol, start_date.isoformat(), end_date.isoformat()),
                )

    def get_shared_daily_history(
        self, symbol: str, start_date: date, end_date: date
    ) -> tuple[DailyBar, ...] | None:
        with self._connect() as db:
            covered = db.execute(
                "SELECT 1 FROM shared_daily_history_ranges WHERE symbol=? "
                "AND start_date=? AND end_date=?",
                (symbol, start_date.isoformat(), end_date.isoformat()),
            ).fetchone()
            if covered is None:
                return None
            rows = db.execute(
                "SELECT bar_json FROM shared_daily_bars WHERE symbol=? "
                "AND timestamp>=? AND timestamp<? ORDER BY timestamp",
                (
                    symbol,
                    start_date.isoformat(),
                    datetime.combine(end_date, datetime.max.time()).isoformat(),
                ),
            ).fetchall()
        return tuple(_daily_bar_from_json(row[0]) for row in rows)

    def cleanup_shared_history(self) -> int:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = (
                db.execute("SELECT count(*) FROM shared_bars").fetchone()[0]
                + db.execute("SELECT count(*) FROM shared_daily_bars").fetchone()[0]
            )
            db.execute("DELETE FROM shared_bars")
            db.execute("DELETE FROM shared_daily_bars")
            db.execute("DELETE FROM shared_daily_history_ranges")
            db.execute("DELETE FROM shared_history_ranges")
        return int(count)

    def latest(self) -> dict[str, object] | None:
        return self.get(None)

    def get(self, run_id: int | None) -> dict[str, object] | None:
        with self._connect() as db:
            if run_id is None:
                row = db.execute(
                    "SELECT result_json, bars_json FROM backtests "
                    "JOIN minute_snapshots USING(snapshot_id) ORDER BY id DESC LIMIT 1"
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT result_json, bars_json FROM backtests "
                    "JOIN minute_snapshots USING(snapshot_id) WHERE id=?",
                    (run_id,),
                ).fetchone()
        if row is None:
            return None
        result: dict[str, object] = json.loads(row[0])
        result["bars"] = json.loads(row[1])
        return result

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA foreign_keys = ON")
        return db


def _daily_bar_json(bar: DailyBar) -> str:
    return _json(
        {
            key: str(value)
            if isinstance(value, Decimal)
            else value.isoformat()
            if isinstance(value, datetime)
            else value
            for key, value in asdict(bar).items()
        }
    )


def _daily_bar_from_json(content: str) -> DailyBar:
    value = json.loads(content)
    for key in ("open", "high", "low", "close", "amount"):
        if value[key] is not None:
            value[key] = Decimal(value[key])
    value["timestamp"] = datetime.fromisoformat(value["timestamp"])
    return DailyBar(**value)


def _bar_json(bar: MinuteBar) -> str:
    return _json(
        {
            key: (
                str(value)
                if isinstance(value, Decimal)
                else value.isoformat()
                if isinstance(value, datetime)
                else value
            )
            for key, value in asdict(bar).items()
        }
    )


def _bar_from_json(content: str) -> MinuteBar:
    value = json.loads(content)
    for key in ("open", "high", "low", "close", "amount"):
        if value[key] is not None:
            value[key] = Decimal(value[key])
    value["timestamp"] = datetime.fromisoformat(value["timestamp"])
    return MinuteBar(**value)


def _bars_json(bars: tuple[MinuteBar, ...]) -> str:
    return "[" + ",".join(_bar_json(bar) for bar in bars) + "]"


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=_json_default
    )


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")
