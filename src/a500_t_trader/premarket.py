"""Advisory-only pre-market planning from user rules and account state."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from .market_data import CapabilityUnavailableError, Snapshot
from .trading_calendar import SessionState

SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_INPUT_AGE = timedelta(minutes=30)


class PlanMarketData(Protocol):
    def get_snapshot(self, symbol: str) -> Snapshot | None: ...

    def session_status(self, session_date: date) -> tuple[SessionState, bool]: ...


@dataclass(frozen=True, slots=True)
class EtfRule:
    symbol: str
    buy_below: Decimal | None
    sell_above: Decimal | None
    max_buy_cash: Decimal
    minimum_holding: int

    def __post_init__(self) -> None:
        if not self.symbol.isdigit() or len(self.symbol) != 6:
            raise ValueError("ETF symbols must be six-digit codes")
        if self.buy_below is None and self.sell_above is None:
            raise ValueError(f"{self.symbol} requires a buy_below or sell_above rule")
        if self.max_buy_cash < 0 or self.minimum_holding < 0:
            raise ValueError("cash limits and minimum holdings cannot be negative")
        if any(value is not None and value <= 0 for value in (self.buy_below, self.sell_above)):
            raise ValueError("rule price levels must be positive")


@dataclass(frozen=True, slots=True)
class PlanConfig:
    rules: tuple[EtfRule, ...]

    def __post_init__(self) -> None:
        symbols = [rule.symbol for rule in self.rules]
        if not symbols or len(symbols) != len(set(symbols)):
            raise ValueError("configuration must contain unique ETF rules")


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    as_of: datetime
    holdings: dict[str, int]
    cost_basis: dict[str, Decimal]
    sellable: dict[str, int]
    available_cash: Decimal

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("account snapshot timestamp must include a timezone")
        if self.available_cash < 0 or any(value < 0 for value in (*self.holdings.values(), *self.sellable.values())):
            raise ValueError("account quantities and available cash cannot be negative")
        if any(value < 0 for value in self.cost_basis.values()):
            raise ValueError("cost basis cannot be negative")


@dataclass(frozen=True, slots=True)
class Suggestion:
    symbol: str
    direction: str
    condition: str
    quantity: int | None
    reason: str
    risk_or_invalidation: str
    signal_strength: int | None
    signal_strength_note: str


@dataclass(frozen=True, slots=True)
class Plan:
    as_of: datetime
    account_source: str
    capability_status: str
    suggestions: tuple[Suggestion, ...]


def generate_plan(
    config: PlanConfig,
    account: AccountSnapshot,
    market_data: PlanMarketData,
    *,
    as_of: datetime,
) -> Plan:
    """Generate explainable suggestions; risk gates always precede quantities."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("planning timestamp must include a timezone")
    plan_time = as_of.astimezone(SHANGHAI)
    try:
        session, calendar_stale = market_data.session_status(plan_time.date())
    except CapabilityUnavailableError:
        session, calendar_stale = SessionState.UNKNOWN, True

    suggestions: list[Suggestion] = []
    data_missing = calendar_stale or session is SessionState.UNKNOWN
    for rule in config.rules:
        required_account_fields = (account.holdings, account.cost_basis, account.sellable)
        if any(rule.symbol not in values for values in required_account_fields):
            data_missing = True
            suggestions.append(_blocked(rule, "required user-entered account fields unavailable"))
            continue
        try:
            snapshot = market_data.get_snapshot(rule.symbol)
        except CapabilityUnavailableError:
            snapshot = None
        if snapshot is None:
            data_missing = True
            suggestions.append(_blocked(rule, "market data capability unavailable or unauthorized; no trade quantity"))
            continue

        market_age = plan_time - snapshot.timestamp.astimezone(SHANGHAI)
        account_age = plan_time - account.as_of.astimezone(SHANGHAI)
        if market_age < timedelta(0) or market_age > MAX_INPUT_AGE or account_age < timedelta(0) or account_age > MAX_INPUT_AGE:
            data_missing = True
            why = "stale market data" if market_age < timedelta(0) or market_age > MAX_INPUT_AGE else "stale account snapshot"
            suggestions.append(_blocked(rule, f"{why}; no tradable quantity may be advised"))
            continue
        if calendar_stale or session is SessionState.UNKNOWN:
            suggestions.append(_blocked(rule, "trading calendar unavailable or stale; risk control veto"))
            continue
        if session is SessionState.CLOSED:
            suggestions.append(_blocked(rule, "not a trading session; risk control veto"))
            continue
        if snapshot.last_price is None or snapshot.last_price <= 0:
            data_missing = True
            suggestions.append(_blocked(rule, "usable market price unavailable; no trade quantity"))
            continue

        price = snapshot.last_price
        if rule.buy_below is not None and price <= rule.buy_below:
            budget = min(account.available_cash, rule.max_buy_cash)
            quantity = int((budget / price / 100).to_integral_value(rounding=ROUND_FLOOR)) * 100
            if quantity == 0:
                suggestions.append(_blocked(rule, "available cash cannot fund one 100-share lot; risk control veto"))
                continue
            suggestions.append(Suggestion(
                rule.symbol, "buy", f"price <= {rule.buy_below} CNY", quantity,
                f"last price {price} CNY meets configured buy-below rule {rule.buy_below} CNY",
                "expires if the price rises above the configured level; subject to user review",
                None, "No signal score is defined by this configured rule.",
            ))
        elif rule.sell_above is not None and price >= rule.sell_above:
            holdings = account.holdings[rule.symbol]
            quantity = min(account.sellable[rule.symbol], max(0, holdings - rule.minimum_holding))
            quantity = quantity // 100 * 100
            if quantity == 0:
                suggestions.append(_blocked(rule, "no eligible sellable quantity above minimum holding; risk control veto"))
                continue
            suggestions.append(Suggestion(
                rule.symbol, "sell", f"price >= {rule.sell_above} CNY", quantity,
                f"last price {price} CNY meets configured sell-above rule {rule.sell_above} CNY",
                "expires if the price falls below the configured level; retains minimum holding",
                None, "No signal score is defined by this configured rule.",
            ))
        else:
            suggestions.append(Suggestion(
                rule.symbol, "none", "configured price condition not met", None,
                f"last price {price} CNY meets neither configured trigger",
                "No trade suggested; review configured rules and current data.", None,
                "No signal score is defined by this configured rule.",
            ))

    status = "unavailable: missing, stale, or unauthorized required input/data" if data_missing else "available"
    return Plan(plan_time, "user-entered; not broker-verified", status, tuple(suggestions))


def _blocked(rule: EtfRule, reason: str) -> Suggestion:
    return Suggestion(rule.symbol, "none", "not evaluated", None, reason, reason, None,
                      "No signal score is defined by this configured rule.")


def load_config(path: str | Path) -> PlanConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(data) != {"etfs"} or not isinstance(data["etfs"], list):
        raise ValueError("configuration must contain only an 'etfs' list")
    return PlanConfig(tuple(EtfRule(
        symbol=item["symbol"],
        buy_below=_optional_decimal(item.get("buy_below")),
        sell_above=_optional_decimal(item.get("sell_above")),
        max_buy_cash=Decimal(str(item.get("max_buy_cash", 0))),
        minimum_holding=int(item.get("minimum_holding", 0)),
    ) for item in data["etfs"]))


def load_account(path: str | Path) -> AccountSnapshot:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(data) != {"as_of", "holdings", "cost_basis", "sellable", "available_cash"}:
        raise ValueError("account snapshot requires as_of, holdings, cost_basis, sellable, and available_cash")
    return AccountSnapshot(
        datetime.fromisoformat(data["as_of"]),
        {key: int(value) for key, value in data["holdings"].items()},
        {key: Decimal(str(value)) for key, value in data["cost_basis"].items()},
        {key: int(value) for key, value in data["sellable"].items()},
        Decimal(str(data["available_cash"])),
    )


def plan_json(plan: Plan) -> str:
    return json.dumps(asdict(plan), ensure_ascii=False, default=_json_default, indent=2)


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _json_default(value: object) -> str:
    if isinstance(value, (Decimal, datetime)):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")
