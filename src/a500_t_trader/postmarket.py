"""Post-market review assembled from saved advice, trigger evidence, and bars."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from .market_data import CapabilityUnavailableError, MinuteBar
from .premarket import AccountSnapshot, Plan, plan_json


class ReviewMarketData(Protocol):
    def get_history_bars(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        interval_minutes: int = 1,
    ) -> tuple[MinuteBar, ...]: ...


@dataclass(frozen=True, slots=True)
class PostMarketReview:
    session_date: str
    plan: dict[str, object] | None
    account_snapshot: dict[str, object]
    recommendations: dict[str, object]
    triggers: tuple[dict[str, object], ...]
    market_data: dict[str, object]
    manual_fills: dict[str, object]
    simulated_fills: dict[str, object]
    missing_data: tuple[str, ...]


def save_plan(path: str | Path, plan: Plan, account: AccountSnapshot) -> None:
    """Save a locally reviewable plan and its separate user-entered account input."""
    account_record = asdict(account)
    payload = {
        "session_date": plan.as_of.date().isoformat(),
        "plan": json.loads(plan_json(plan)),
        "account_snapshot": account_record,
    }
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, default=_json_default, indent=2) + "\n",
        encoding="utf-8",
    )


def generate_review(
    session_date: date,
    plan_path: str | Path,
    evidence_path: str | Path,
    market_data: ReviewMarketData | None,
    market_data_error: str | None = None,
) -> PostMarketReview:
    missing: list[str] = []
    plan_record: dict[str, object] | None = None
    account_record: dict[str, object] = {
        "status": "not saved",
        "source": "unavailable",
    }
    try:
        saved = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            missing.append("saved pre-market plan is not a JSON object")
        elif saved.get("session_date") != session_date.isoformat():
            missing.append("saved pre-market plan does not match the requested session date")
        elif not isinstance(saved.get("plan"), dict):
            missing.append("saved pre-market plan has no valid plan record")
        else:
            plan_record = saved["plan"]
            snapshot = saved.get("account_snapshot")
            if isinstance(snapshot, dict):
                account_record = snapshot
            else:
                account_record = {
                    "status": "not included or invalid in saved plan",
                    "source": "unavailable",
                }
                missing.append("saved account snapshot unavailable or invalid")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        missing.append(f"saved pre-market plan unavailable: {error}")

    recommendations: dict[str, object]
    if plan_record is None:
        recommendations = {
            "classification": "advisory recommendations; not orders",
            "records": [],
        }
    else:
        recommendations = {
            "classification": "advisory recommendations; not orders",
            "records": plan_record.get("suggestions", []),
        }

    triggers: list[dict[str, object]] = []
    try:
        with sqlite3.connect(
            f"file:{Path(evidence_path).resolve()}?mode=ro", uri=True
        ) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM trigger_evidence WHERE substr(bar_timestamp, 1, 10) = ? "
                "ORDER BY bar_timestamp, symbol, direction",
                (session_date.isoformat(),),
            )
            triggers = [dict(row) for row in rows]
        suggestions = recommendations["records"]
        if isinstance(suggestions, list):
            for trigger in triggers:
                trigger["related_recommendation_indices"] = [
                    index
                    for index, suggestion in enumerate(suggestions)
                    if isinstance(suggestion, dict)
                    and suggestion.get("symbol") == trigger.get("symbol")
                    and suggestion.get("direction") == trigger.get("direction")
                ]
    except (sqlite3.Error, OSError) as error:
        missing.append(f"trigger evidence unavailable: {error}")
    if not triggers and not any("trigger evidence unavailable" in item for item in missing):
        missing.append("no trigger evidence recorded for the requested session date")

    market: dict[str, object] = {}
    symbols = _symbols(plan_record, triggers)
    if market_data is None:
        detail = f": {market_data_error}" if market_data_error else ""
        missing.append(f"historical market data unavailable or not supplied{detail}")
    else:
        for symbol in symbols:
            try:
                bars = market_data.get_history_bars(
                    symbol, session_date, session_date, interval_minutes=1
                )
            except CapabilityUnavailableError as error:
                missing.append(
                    f"{symbol} historical market data unavailable or unauthorized: {error}"
                )
                continue
            except Exception as error:
                missing.append(f"{symbol} historical market data unavailable: {error}")
                continue
            valid = [
                bar for bar in bars
                if bar.symbol == symbol
                and bar.interval_minutes == 1
                and bar.timestamp.date() == session_date
            ]
            if not valid:
                missing.append(
                    f"{symbol} has no historical 1-minute market bars for {session_date}"
                )
                continue
            last = max(valid, key=lambda bar: bar.timestamp)
            market[symbol] = {
                "bar_timestamp": last.timestamp.isoformat(),
                "open": str(last.open), "high": str(last.high),
                "low": str(last.low), "close": str(last.close),
                "volume": last.volume,
                "amount": None if last.amount is None else str(last.amount),
                "is_completed": last.is_completed,
                "interpretation": "latest available 1-minute bar; not necessarily an official close",
            }
    if not symbols:
        missing.append("no ETF symbols available from the saved plan or trigger evidence")

    if account_record.get("status") == "not saved" or account_record.get("source") == "unavailable":
        account_record = {
            **account_record,
            "classification": "user-entered account snapshot; not broker-verified",
        }
    else:
        account_record = {
            **account_record,
            "source": "user-entered; not broker-verified",
            "classification": "account snapshot; not recommendation, order, or fill",
        }
    return PostMarketReview(
        session_date.isoformat(), plan_record, account_record, recommendations,
        tuple(triggers), market,
        {"status": "not recorded", "records": []},
        {"status": "not recorded", "records": []}, tuple(missing),
    )


def review_json(review: PostMarketReview) -> str:
    return json.dumps(asdict(review), ensure_ascii=False, default=_json_default, indent=2)


def _symbols(
    plan: dict[str, object] | None, triggers: list[dict[str, object]]
) -> tuple[str, ...]:
    symbols: set[str] = {
        str(item["symbol"]) for item in triggers if item.get("symbol")
    }
    if plan:
        for suggestion in plan.get("suggestions", []):
            if isinstance(suggestion, dict) and suggestion.get("symbol"):
                symbols.add(str(suggestion["symbol"]))
    return tuple(sorted(symbols))


def _json_default(value: object) -> str:
    if isinstance(value, (Decimal, datetime)):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")
