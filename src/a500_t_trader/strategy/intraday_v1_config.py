"""Explicit, auditable inputs for the V1 intraday ETF T strategy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

STRATEGY_ID = "intraday-etf-t-v1"

_REQUIRED_TOP_LEVEL = (
    "strategy_id",
    "rule_version",
    "implementation_version",
    "rule_source",
    "rule_source_sha256",
    "parameters",
    "certification",
    "fees",
    "account",
    "securities",
)
_REQUIRED_PARAMETER_SECTIONS = (
    "indicators",
    "trend",
    "scores",
    "signals",
    "grid",
    "risk",
    "liquidity",
    "timing",
)
_REQUIRED_CERTIFICATION_FIELDS = (
    "net_return_minimum",
    "paired_trade_average_net_profit_minimum",
    "profit_factor_minimum",
    "in_sample_profit_factor_minimum",
    "out_of_sample_profit_factor_minimum",
    "maximum_drawdown",
    "minimum_sample_count",
    "cost_slippage_stress_multiplier",
    "walk_forward",
    "neighbor_parameter_perturbation",
)
_REQUIRED_FEE_FIELDS = (
    "commission_rate",
    "minimum_commission",
    "impact_bps",
    "source",
    "as_of",
)
_REQUIRED_ACCOUNT_FIELDS = ("as_of", "cash", "positions")
_REQUIRED_POSITION_FIELDS = (
    "quantity",
    "protected_floor",
    "cost_basis",
    "sellable_quantity",
    "market_value",
)
_REQUIRED_SECURITY_FIELDS = (
    "t_plus",
    "lot_size",
    "tick_size",
    "lower_price_limit",
    "upper_price_limit",
    "source",
    "as_of",
)


@dataclass(frozen=True, slots=True)
class IntradayV1Config:
    """User-supplied V1 inputs and their canonical content fingerprint."""

    strategy_id: str
    rule_version: str
    implementation_version: str
    rule_source: str
    rule_source_sha256: str
    parameters: dict[str, object]
    certification: dict[str, object]
    fees: dict[str, object]
    account: dict[str, object]
    securities: dict[str, dict[str, object]]
    fingerprint: str


def load_intraday_v1_config(path: str | Path) -> IntradayV1Config:
    """Load the complete V1 configuration without supplying implicit values."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    root = _object(document, "configuration")
    _require(root, _REQUIRED_TOP_LEVEL, "")

    strategy_id = _text(root["strategy_id"], "strategy_id")
    if strategy_id != STRATEGY_ID:
        raise ValueError(f"strategy_id must be {STRATEGY_ID!r}")
    rule_version = _text(root["rule_version"], "rule_version")
    implementation_version = _text(
        root["implementation_version"], "implementation_version"
    )
    rule_source = _text(root["rule_source"], "rule_source")
    rule_source_sha256 = _text(root["rule_source_sha256"], "rule_source_sha256")
    if len(rule_source_sha256) != 64 or any(
        char not in "0123456789abcdefABCDEF" for char in rule_source_sha256
    ):
        raise ValueError("rule_source_sha256 must be a 64-character SHA-256 digest")
    parameters = _object(root["parameters"], "parameters")
    _require(parameters, _REQUIRED_PARAMETER_SECTIONS, "parameters")
    for section in _REQUIRED_PARAMETER_SECTIONS:
        if not isinstance(parameters[section], dict) or not parameters[section]:
            raise ValueError(f"parameters.{section} must contain explicit rule values")
    certification = _object(root["certification"], "certification")
    _require(certification, _REQUIRED_CERTIFICATION_FIELDS, "certification")
    fees = _object(root["fees"], "fees")
    _require(fees, _REQUIRED_FEE_FIELDS, "fees")
    _text(fees["source"], "fees.source")
    _timestamp(fees["as_of"], "fees.as_of")

    account = _object(root["account"], "account")
    _require(account, _REQUIRED_ACCOUNT_FIELDS, "account")
    _timestamp(account["as_of"], "account.as_of")
    _object(account["positions"], "account.positions")
    positions = cast(dict[str, object], account["positions"])
    for symbol, position_value in positions.items():
        _symbol(symbol, f"account.positions.{symbol}")
        position = _object(position_value, f"account.positions.{symbol}")
        _require(position, _REQUIRED_POSITION_FIELDS, f"account.positions.{symbol}")

    securities_data = _object(root["securities"], "securities")
    if not securities_data:
        raise ValueError("missing required configuration: securities (at least one)")
    securities: dict[str, dict[str, object]] = {}
    for symbol, security_value in securities_data.items():
        _symbol(symbol, f"securities.{symbol}")
        security = _object(security_value, f"securities.{symbol}")
        _require(security, _REQUIRED_SECURITY_FIELDS, f"securities.{symbol}")
        if security["t_plus"] not in ("T+0", "T+1"):
            raise ValueError(
                f"securities.{symbol}.t_plus must be explicitly T+0 or T+1"
            )
        _text(security["source"], f"securities.{symbol}.source")
        _timestamp(security["as_of"], f"securities.{symbol}.as_of")
        securities[symbol] = security
    extra_positions = positions.keys() - securities.keys()
    if extra_positions:
        symbol = sorted(extra_positions)[0]
        raise ValueError(f"missing required configuration: securities.{symbol}")

    canonical = json.dumps(
        root, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return IntradayV1Config(
        strategy_id,
        rule_version,
        implementation_version,
        rule_source,
        rule_source_sha256.lower(),
        parameters,
        certification,
        fees,
        account,
        securities,
        hashlib.sha256(canonical).hexdigest(),
    )


def _object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{path} must be a JSON object")
    return cast(dict[str, object], value)


def _require(data: dict[str, object], fields: tuple[str, ...], prefix: str) -> None:
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == {} or value == []:
            name = f"{prefix}.{field}" if prefix else field
            raise ValueError(f"missing required configuration: {name}")


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be explicitly configured as non-empty text")
    return value


def _timestamp(value: object, path: str) -> None:
    text = _text(value, path)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{path} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{path} must include a timezone")


def _symbol(value: str, path: str) -> None:
    if len(value) != 6 or not value.isdigit():
        raise ValueError(f"{path} must be a six-digit security code")
