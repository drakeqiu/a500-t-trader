"""User-started intraday condition monitoring and persisted trigger evidence."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol, TextIO
from zoneinfo import ZoneInfo

import psycopg

from ..database import initialize_database
from ..market_data import CapabilityUnavailableError, MinuteBar
from ..market_data.trading_calendar import SessionState
from .premarket import MAX_INPUT_AGE, AccountSnapshot, EtfRule, PlanConfig

SHANGHAI = ZoneInfo("Asia/Shanghai")


class MonitorMarketData(Protocol):
    def get_intraday_bars(
        self, symbol: str, interval_minutes: int = 1
    ) -> tuple[MinuteBar, ...]: ...

    def session_status(self, session_date: date) -> tuple[SessionState, bool]: ...


@dataclass(frozen=True, slots=True)
class Trigger:
    symbol: str
    direction: str
    condition: str
    risk_or_invalidation: str
    bar: MinuteBar
    evaluated_at: datetime
    account_as_of: datetime
    account_source: str = "user-entered; not broker-verified"


class TriggerEvidenceStore:
    """Append-only evidence for each rule match and normalized minute bar."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        initialize_database(dsn)

    def record(self, trigger: Trigger, session: SessionState) -> bool:
        bar = trigger.bar
        with psycopg.connect(self.dsn) as connection:
            result = connection.execute(
                """INSERT INTO trigger_evidence (
                    symbol, direction, condition, bar_timestamp,
                    bar_interval_minutes, bar_open, bar_high, bar_low, bar_close,
                    bar_volume, bar_amount, bar_completed, bar_source, session_state,
                    evaluated_at, risk_or_invalidation, account_as_of, account_source
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING""",
                (
                    trigger.symbol,
                    trigger.direction,
                    trigger.condition,
                    bar.timestamp.isoformat(),
                    bar.interval_minutes,
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    bar.volume,
                    None if bar.amount is None else str(bar.amount),
                    int(bar.is_completed),
                    bar.source,
                    session.value,
                    trigger.evaluated_at.isoformat(),
                    trigger.risk_or_invalidation,
                    trigger.account_as_of.isoformat(),
                    trigger.account_source,
                ),
            )
            return result.rowcount == 1


def monitor_once(
    config: PlanConfig,
    account: AccountSnapshot,
    market_data: MonitorMarketData,
    evidence: TriggerEvidenceStore,
    *,
    as_of: datetime,
    output: TextIO | None = None,
    errors: TextIO | None = None,
) -> int:
    """Evaluate one foreground monitoring pass and save/print new triggers."""
    output = sys.stdout if output is None else output
    errors = sys.stderr if errors is None else errors
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("monitor timestamp must include a timezone")
    now = as_of.astimezone(SHANGHAI)
    try:
        session, stale = market_data.session_status(now.date())
    except CapabilityUnavailableError as error:
        print(f"Required trading-calendar capability unavailable: {error}", file=errors)
        return 0
    if stale or session is SessionState.UNKNOWN:
        print(
            "Trading calendar unavailable or stale; conditions not evaluated.",
            file=errors,
        )
        return 0
    if session is SessionState.CLOSED:
        print("Trading session is closed; no conditions evaluated.", file=output)
        return 0
    account_age = now - account.as_of.astimezone(SHANGHAI)
    if account_age < timedelta(0) or account_age > MAX_INPUT_AGE:
        print(
            "Account snapshot is stale or future-dated; no conditions evaluated.",
            file=errors,
        )
        return 0

    saved = 0
    for rule in config.rules:
        try:
            bars = market_data.get_intraday_bars(rule.symbol, interval_minutes=1)
        except CapabilityUnavailableError as error:
            print(
                f"{rule.symbol}: required 1-minute market-data capability unavailable "
                f"or unauthorized: {error}",
                file=errors,
            )
            continue
        if not bars:
            print(
                f"{rule.symbol}: no 1-minute bars available; no condition evaluated.",
                file=errors,
            )
            continue
        latest_by_status: dict[bool, MinuteBar] = {}
        for bar in bars:
            if bar.symbol != rule.symbol or bar.interval_minutes != 1:
                print(
                    f"{rule.symbol}: provider returned an unexpected normalized bar.",
                    file=errors,
                )
                continue
            current = latest_by_status.get(bar.is_completed)
            if current is None or bar.timestamp > current.timestamp:
                latest_by_status[bar.is_completed] = bar
        newest_bar = max(bars, key=lambda item: item.timestamp)
        age = now - newest_bar.timestamp
        if age < timedelta(0) or age > MAX_INPUT_AGE:
            print(
                f"{rule.symbol}: latest 1-minute bar is stale or future-dated; "
                "required intraday data unavailable, condition not evaluated.",
                file=errors,
            )
            continue
        for bar in sorted(latest_by_status.values(), key=lambda item: item.timestamp):
            for trigger in _triggers(rule, bar, now, account.as_of):
                if not _account_allows(rule, trigger.direction, account):
                    print(
                        f"{rule.symbol}: account limits vetoed "
                        f"the {trigger.direction} trigger.",
                        file=errors,
                    )
                    continue
                if not evidence.record(trigger, session):
                    continue
                saved += 1
                completion = (
                    "" if bar.is_completed else " [UNFINISHED BAR — provisional]"
                )
                print(
                    f"SUGGESTION{completion}: {trigger.direction} {trigger.symbol}; "
                    f"condition: {trigger.condition}; bar: "
                    f"{bar.timestamp.isoformat()}; "
                    f"risk/invalidation: {trigger.risk_or_invalidation}. NOT AN ORDER; "
                    "no trade was executed.",
                    file=output,
                )
    if not saved:
        print("No new trigger.", file=output)
    return saved


def run_monitor(
    config: PlanConfig,
    account: AccountSnapshot,
    market_data: MonitorMarketData,
    database: str,
    *,
    poll_seconds: float = 5.0,
    once: bool = False,
    as_of: datetime | None = None,
) -> None:
    """Run synchronously in the invoking process; Ctrl-C ends monitoring."""
    if poll_seconds <= 0:
        raise ValueError("poll interval must be positive")
    evidence = TriggerEvidenceStore(database)
    print("Intraday monitoring started in this process; press Ctrl-C to stop.")
    while True:
        monitor_once(
            config,
            account,
            market_data,
            evidence,
            as_of=as_of if once and as_of is not None else datetime.now(SHANGHAI),
        )
        if once:
            return
        time.sleep(poll_seconds)


def _triggers(
    rule: EtfRule, bar: MinuteBar, now: datetime, account_as_of: datetime
) -> tuple[Trigger, ...]:
    """Emit each advisory trigger whose configured level the bar crossed."""
    found: list[Trigger] = []
    if rule.buy_below is not None and bar.low <= rule.buy_below:
        risk = (
            f"expires if a later price rises above {rule.buy_below} CNY; "
            "user review required"
            if bar.is_completed
            else "provisional while the minute bar is unfinished; user review required"
        )
        found.append(
            Trigger(
                rule.symbol,
                "buy",
                f"low <= {rule.buy_below} CNY",
                risk,
                bar,
                now,
                account_as_of,
            )
        )
    if rule.sell_above is not None and bar.high >= rule.sell_above:
        risk = (
            f"expires if a later price falls below {rule.sell_above} CNY; "
            "user review required"
            if bar.is_completed
            else "provisional while the minute bar is unfinished; user review required"
        )
        found.append(
            Trigger(
                rule.symbol,
                "sell",
                f"high >= {rule.sell_above} CNY",
                risk,
                bar,
                now,
                account_as_of,
            )
        )
    return tuple(found)


def _account_allows(rule: EtfRule, direction: str, account: AccountSnapshot) -> bool:
    if direction == "buy":
        threshold = rule.buy_below
        if threshold is None:
            return False
        cash_limit = min(
            account.available_cash,
            rule.max_buy_cash
            if rule.max_buy_cash is not None
            else account.available_cash,
        )
        return int(cash_limit / threshold / 100) > 0
    sellable = min(
        account.sellable.get(rule.symbol, 0),
        max(0, account.holdings.get(rule.symbol, 0) - rule.minimum_holding),
    )
    return sellable // 100 > 0
