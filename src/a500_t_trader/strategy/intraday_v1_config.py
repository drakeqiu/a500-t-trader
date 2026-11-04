"""Explicit, auditable inputs for the V1 intraday ETF T strategy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

CONFIG_SCHEMA_VERSION = "2.0"
STRATEGY_ID = "intraday-etf-t-v1-rsi"
RULE_SOURCE = (
    "docs/strategy/intraday-etf-t-v1/intraday_etf_t_quantitative_strategy_v1.0.md"
)
RULE_SOURCE_SHA256 = "769e2cd8d259074d3165f7c7ced0128b9658fcf37a13e634faad5cc4b980b2c6"
_REQUIRED_TOP_LEVEL = (
    "schema_version",
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

    @property
    def schema_version(self) -> str:
        return CONFIG_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class LiveSignalConfig:
    etfs: tuple[str, ...]
    parameters: dict[str, object]
    costs: dict[str, dict[str, object]]
    fingerprint: str


DEFAULT_LIVE_CONFIG_PATH = Path("config/intraday-signals.yaml")


def load_live_signal_config(path: Path = DEFAULT_LIVE_CONFIG_PATH) -> LiveSignalConfig:
    """Load optional ETF scope and parameter overrides over immutable V1 defaults."""
    from copy import deepcopy

    from .intraday_technical import TECHNICAL_PARAMETERS

    parameters = deepcopy(TECHNICAL_PARAMETERS)
    etfs: tuple[str, ...] = ()
    costs: dict[str, dict[str, object]] = {}
    try:
        import yaml
    except ImportError as error:
        raise ValueError("YAML configuration requires the PyYAML package") from error
    if not path.exists():
        canonical = json.dumps(
            {"etfs": etfs, "parameters": parameters, "costs": costs},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return LiveSignalConfig(
            etfs,
            parameters,
            costs,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"cannot load live signal YAML: {error}") from None
    if not isinstance(document, dict) or set(document) - {
        "etfs",
        "parameters",
        "costs",
    }:
        raise ValueError("live YAML accepts only etfs, parameters, and costs")
    raw_etfs = document.get("etfs")
    if (
        not isinstance(raw_etfs, list)
        or not raw_etfs
        or any(
            not isinstance(item, (str, int)) or isinstance(item, bool)
            for item in raw_etfs
        )
    ):
        raise ValueError("etfs must be a non-empty list of six-digit ETF codes")
    etfs = tuple(dict.fromkeys(str(item) for item in raw_etfs))
    if any(len(symbol) != 6 or not symbol.isdigit() for symbol in etfs):
        raise ValueError("etfs must contain only six-digit ETF codes")
    overrides = document.get("parameters", {})
    if not isinstance(overrides, dict):
        raise ValueError("parameters must be a mapping")
    _merge_live_parameters(parameters, overrides, "parameters")
    _validate_score_threshold_intervals(parameters["scores"])
    costs = _parse_live_costs(document.get("costs", {}), etfs)
    canonical = json.dumps(
        {"etfs": etfs, "parameters": parameters, "costs": costs},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return LiveSignalConfig(
        etfs,
        parameters,
        costs,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _parse_live_costs(
    raw_costs: object, etfs: tuple[str, ...]
) -> dict[str, dict[str, object]]:
    if not isinstance(raw_costs, dict):
        raise ValueError("costs must be a mapping keyed by ETF code")
    costs: dict[str, dict[str, object]] = {}
    expected = {
        "tick_size",
        "commission_rate",
        "minimum_commission",
        "reference_quantity",
    }
    for raw_symbol, raw_values in raw_costs.items():
        symbol = str(raw_symbol)
        if symbol not in etfs:
            raise ValueError(f"costs.{symbol} must name an ETF in etfs")
        if not isinstance(raw_values, dict) or set(raw_values) != expected:
            raise ValueError(
                f"costs.{symbol} must define exactly {', '.join(sorted(expected))}"
            )
        values: dict[str, object] = {}
        for key in ("tick_size", "commission_rate", "minimum_commission"):
            raw_value = raw_values[key]
            if isinstance(raw_value, bool):
                raise ValueError(f"costs.{symbol}.{key} must be a decimal number")
            try:
                value = Decimal(str(raw_value))
            except InvalidOperation, TypeError, ValueError:
                raise ValueError(
                    f"costs.{symbol}.{key} must be a decimal number"
                ) from None
            if not value.is_finite():
                raise ValueError(f"costs.{symbol}.{key} must be finite")
            if key == "tick_size" and value <= 0:
                raise ValueError(f"costs.{symbol}.tick_size must be positive")
            if key != "tick_size" and value < 0:
                raise ValueError(f"costs.{symbol}.{key} cannot be negative")
            values[key] = value
        quantity = raw_values["reference_quantity"]
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
            or quantity % 100
        ):
            raise ValueError(
                f"costs.{symbol}.reference_quantity must be a positive multiple of 100"
            )
        values["reference_quantity"] = quantity
        costs[symbol] = values
    return costs


def _merge_live_parameters(
    defaults: dict[str, object], overrides: dict[object, object], path: str
) -> None:
    for key, value in overrides.items():
        if not isinstance(key, str) or key not in defaults:
            raise ValueError(f"unknown V1 parameter: {path}.{key}")
        default = defaults[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                raise ValueError(f"{path}.{key} must be a mapping")
            _merge_live_parameters(default, value, f"{path}.{key}")
        elif isinstance(default, list):
            _validate_live_list(path, key, value, default)
            defaults[key] = value
        elif type(value) is not type(default) and not (
            isinstance(default, (int, float))
            and not isinstance(default, bool)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            raise ValueError(f"{path}.{key} has an invalid value type")
        else:
            _validate_live_leaf(f"{path}.{key}", key, value)
            defaults[key] = value


def _validate_live_list(
    parent: str, key: str, value: object, expected: list[object]
) -> None:
    path = f"{parent}.{key}"
    if not isinstance(value, list) or len(value) != len(expected):
        raise ValueError(f"{path} must match the default list shape")
    for index, (item, default) in enumerate(zip(value, expected, strict=True)):
        item_path = f"{path}[{index}]"
        if isinstance(default, list):
            _validate_live_list(path, str(index), item, default)
        else:
            if type(item) is not type(default):
                raise ValueError(f"{item_path} contains invalid values")
            _validate_live_leaf(item_path, key, item)


def _validate_live_leaf(path: str, key: str, value: object) -> None:
    from datetime import datetime
    from decimal import Decimal, InvalidOperation

    if isinstance(value, str) and (".timing." in path or key == "anchor"):
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError:
            raise ValueError(f"{path} must use HH:MM time format") from None
        return
    if isinstance(value, bool):
        raise ValueError(f"{path} must be a non-negative finite number")
    try:
        number = Decimal(str(value))
    except InvalidOperation, ValueError:
        raise ValueError(f"{path} must be a non-negative finite number") from None
    if not number.is_finite() or number < 0:
        raise ValueError(f"{path} must be a non-negative finite number")
    if "indicators." in path and number <= 0:
        raise ValueError(f"{path} must be greater than zero")
    if path.startswith("parameters.scores.") and number > 100:
        raise ValueError(f"{path} must not exceed 100")
    if (
        path.startswith("parameters.grid.")
        and key in {"levels", "minimum_ticks"}
        and number <= 0
    ):
        raise ValueError(f"{path} must be greater than zero")


def _validate_score_threshold_intervals(scores: object) -> None:
    from decimal import Decimal

    if not isinstance(scores, dict):
        return
    for full_key, full_value in scores.items():
        if not isinstance(full_key, str) or not full_key.endswith("_full"):
            continue
        start_key = f"{full_key[:-5]}_start"
        if start_key not in scores:
            continue
        start = Decimal(str(scores[start_key]))
        full = Decimal(str(full_value))
        if full <= start:
            raise ValueError(
                f"parameters.scores.{full_key} must be greater than "
                f"parameters.scores.{start_key}"
            )


def load_intraday_v1_config(path: str | Path) -> IntradayV1Config:
    """Load the complete V1 configuration without supplying implicit values."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    root = _object(document, "configuration")
    schema_version = _text(root.get("schema_version"), "schema_version")
    if schema_version != CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schema_version {schema_version!r}; "
            f"expected {CONFIG_SCHEMA_VERSION!r}"
        )
    _require(root, _REQUIRED_TOP_LEVEL, "")

    strategy_id = _text(root["strategy_id"], "strategy_id")
    if strategy_id != STRATEGY_ID:
        raise ValueError(f"strategy_id must be {STRATEGY_ID!r}")
    rule_version = _text(root["rule_version"], "rule_version")
    if rule_version != "V1.0":
        raise ValueError("rule_version must be 'V1.0' for the RSI ruleset")
    implementation_version = _text(
        root["implementation_version"], "implementation_version"
    )
    rule_source = _text(root["rule_source"], "rule_source")
    if rule_source != RULE_SOURCE:
        raise ValueError(f"rule_source must be {RULE_SOURCE!r}")
    rule_source_sha256 = _text(root["rule_source_sha256"], "rule_source_sha256")
    if len(rule_source_sha256) != 64 or any(
        char not in "0123456789abcdefABCDEF" for char in rule_source_sha256
    ):
        raise ValueError("rule_source_sha256 must be a 64-character SHA-256 digest")
    if rule_source_sha256.lower() != RULE_SOURCE_SHA256:
        raise ValueError(
            "rule_source_sha256 does not match the tracked RSI strategy source"
        )
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
