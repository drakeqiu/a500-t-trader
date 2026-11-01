"""Advisory-only pre-market planning from user rules and account state."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from ..market_data import CapabilityUnavailableError, Snapshot
from ..market_data.trading_calendar import SessionState

SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_INPUT_AGE = timedelta(minutes=30)


class PlanMarketData(Protocol):
    def get_snapshot(self, symbol: str) -> Snapshot | None: ...

    def session_status(self, session_date: date) -> tuple[SessionState, bool]: ...


@dataclass(frozen=True, slots=True)
class EtfRule:
    symbol: str
    buy_below: Decimal | None = None
    sell_above: Decimal | None = None
    max_buy_cash: Decimal | None = None
    minimum_holding: int = 0

    def __post_init__(self) -> None:
        if not self.symbol.isdigit() or len(self.symbol) != 6:
            raise ValueError("ETF symbols must be six-digit codes")
        if self.buy_below is None and self.sell_above is None:
            raise ValueError("each ETF rule requires buy_below or sell_above")
        if self.buy_below is not None and self.buy_below <= 0:
            raise ValueError("buy_below must be positive")
        if self.sell_above is not None and self.sell_above <= 0:
            raise ValueError("sell_above must be positive")
        if (
            self.buy_below is not None
            and self.sell_above is not None
            and self.buy_below >= self.sell_above
        ):
            raise ValueError("buy_below must be below sell_above")
        if self.max_buy_cash is not None and self.max_buy_cash < 0:
            raise ValueError("max_buy_cash cannot be negative")
        if self.minimum_holding < 0 or self.minimum_holding % 100:
            raise ValueError("minimum_holding must be a nonnegative board lot")

    def triggered_direction(self, price: Decimal) -> str:
        if self.buy_below is not None and price <= self.buy_below:
            return "buy"
        if self.sell_above is not None and price >= self.sell_above:
            return "sell"
        return "none"


@dataclass(frozen=True, slots=True)
class PlanConfig:
    rules: tuple[EtfRule, ...]

    def __post_init__(self) -> None:
        symbols = [rule.symbol for rule in self.rules]
        if not symbols or len(symbols) != len(set(symbols)):
            raise ValueError("configuration must contain unique ETF symbols")


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
        if self.available_cash < 0 or any(
            value < 0 for value in (*self.holdings.values(), *self.sellable.values())
        ):
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
        calendar_ok = not calendar_stale and session is SessionState.TRADING
        calendar_status = "available" if calendar_ok else "unavailable or not trading"
    except CapabilityUnavailableError:
        calendar_ok = False
        calendar_status = "unavailable"
    account_unavailable = False
    suggestions: list[Suggestion] = []
    market_unavailable = False
    for rule in config.rules:
        try:
            snapshot = market_data.get_snapshot(rule.symbol)
        except CapabilityUnavailableError:
            snapshot = None
        price = None if snapshot is None else snapshot.last_price
        reasons: list[str] = []
        account_age = plan_time - account.as_of.astimezone(SHANGHAI)
        if account_age < timedelta(0) or account_age > MAX_INPUT_AGE:
            account_unavailable = True
            reasons.append("account snapshot is stale or future-dated")
        if snapshot is None or price is None or price <= 0:
            reasons.append("market data unavailable")
            market_unavailable = True
        else:
            market_age = plan_time - snapshot.timestamp.astimezone(SHANGHAI)
            if market_age < timedelta(0) or market_age > MAX_INPUT_AGE:
                market_unavailable = True
                reasons.append(
                    "stale market data unavailable: future-dated "
                    "or older than 30 minutes"
                )
        if not calendar_ok:
            reasons.append("trading calendar unavailable or not an active session")
        safe_price = cast(Decimal, price)
        direction = "none" if reasons else rule.triggered_direction(safe_price)
        quantity: int | None = None
        condition = "no configured threshold met"
        reason = "Current price is outside configured thresholds."
        if direction == "buy":
            threshold = rule.buy_below
            condition = f"price <= {threshold} CNY"
            reason = (
                f"last price {price} CNY meets configured buy-below rule "
                f"{threshold} CNY"
            )
            cash_limit = min(
                account.available_cash,
                rule.max_buy_cash
                if rule.max_buy_cash is not None
                else account.available_cash,
            )
            quantity = int(cash_limit / safe_price / 100) * 100
            if (
                rule.symbol not in account.holdings
                or rule.symbol not in account.sellable
            ):
                account_unavailable = True
                direction, quantity = "none", None
                reasons.append(
                    "account fields unavailable: position or sellable quantity missing"
                )
            elif quantity == 0:
                direction, quantity = "none", None
                reasons.append(
                    "available cash is below one board lot or max_buy_cash is zero"
                )
        elif direction == "sell":
            threshold = rule.sell_above
            condition = f"price >= {threshold} CNY"
            reason = f"last price {price} CNY meets sell-above rule {threshold} CNY"
            permitted = max(
                0, account.holdings.get(rule.symbol, 0) - rule.minimum_holding
            )
            quantity = min(account.sellable.get(rule.symbol, 0), permitted) // 100 * 100
            if quantity == 0:
                direction, quantity = "none", None
                reasons.append("no sellable board lot remains above minimum holding")
        elif reasons:
            reason = "; ".join(reasons)
        risk = (
            "; ".join(reasons)
            if reasons
            else (
                f"Configured threshold; minimum holding is "
                f"{rule.minimum_holding} shares; "
                "review manually before any action."
            )
        )
        suggestions.append(
            Suggestion(
                rule.symbol,
                direction,
                condition,
                quantity,
                reason,
                risk,
                None,
                "No signal score is defined; threshold rules are binary, not scored.",
            )
        )
    status = f"{calendar_status}; market data "
    status += "unavailable: stale/missing" if market_unavailable else "available"
    status += "; account fields unavailable" if account_unavailable else ""
    return Plan(
        plan_time, "user-entered; not broker-verified", status, tuple(suggestions)
    )


def load_config(path: str | Path) -> PlanConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(data) != {"etfs"} or not isinstance(data["etfs"], list):
        raise ValueError("configuration must contain only an 'etfs' list")
    rules = []
    fields = {"symbol", "buy_below", "sell_above", "max_buy_cash", "minimum_holding"}
    for item in data["etfs"]:
        if not isinstance(item, dict) or "symbol" not in item or set(item) - fields:
            raise ValueError(
                "ETF entries require symbol and only documented price/limit fields"
            )
        rules.append(
            EtfRule(
                item["symbol"],
                _optional_decimal(item.get("buy_below")),
                _optional_decimal(item.get("sell_above")),
                _optional_decimal(item.get("max_buy_cash")),
                int(item.get("minimum_holding", 0)),
            )
        )
    return PlanConfig(tuple(rules))


def load_account(path: str | Path) -> AccountSnapshot:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(data) != {"as_of", "holdings", "cost_basis", "sellable", "available_cash"}:
        raise ValueError(
            "account snapshot requires as_of, holdings, cost_basis, sellable, "
            "and available_cash"
        )
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
