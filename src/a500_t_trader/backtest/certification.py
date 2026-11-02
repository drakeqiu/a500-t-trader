"""Fail-closed certification for explicitly configured V1 backtest evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

_REQUIRED_CONFIG = (
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
_REQUIRED_BINDING = (
    "rule_source_sha256",
    "rule_version",
    "parameters",
    "implementation_version",
    "data_snapshot_sha256",
    "cost_model",
)


@dataclass(frozen=True, slots=True)
class CertificationEvidence:
    """Computed performance metrics; absent metrics are deliberately represented as None."""

    net_return: Decimal | None
    paired_trade_net_profits: tuple[Decimal, ...]
    profit_factor: Decimal | None
    in_sample_profit_factor: Decimal | None
    out_of_sample_profit_factor: Decimal | None
    maximum_drawdown: Decimal | None
    sample_count: int | None
    stressed_net_return: Decimal | None
    stressed_profit_factor: Decimal | None
    stressed_cost_slippage_multiplier: Decimal | None
    neighbor_net_returns: tuple[Decimal, ...]
    neighbor_profit_factors: tuple[Decimal, ...]
    walk_forward_net_returns: tuple[Decimal, ...]
    walk_forward_profit_factors: tuple[Decimal, ...]


@dataclass(frozen=True, slots=True)
class CertificationResult:
    certified: bool
    fingerprint: str | None
    blockers: tuple[str, ...]
    disclaimer: str = "Historical certification is not a guarantee of future profitability."


def certify_intraday_backtest(
    evidence: CertificationEvidence,
    binding: Mapping[str, object],
    config: Mapping[str, object],
) -> CertificationResult:
    """Evaluate fully supplied metrics and bind any pass to exact certification inputs."""
    blockers: list[str] = []
    thresholds = _thresholds(config, blockers)
    if not isinstance(binding, Mapping):
        blockers.append("binding is incomplete")
    else:
        for name in _REQUIRED_BINDING:
            value = binding.get(name)
            if value is None or value == "" or value == {}:
                blockers.append(f"binding.{name} is incomplete")
        for name in ("rule_source_sha256", "data_snapshot_sha256"):
            digest = binding.get(name)
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in digest)
            ):
                blockers.append(f"binding.{name} must be a SHA-256 digest")
    if thresholds is not None:
        _evaluate(evidence, thresholds, blockers)
    if blockers:
        return CertificationResult(False, None, tuple(dict.fromkeys(blockers)))
    return CertificationResult(True, _fingerprint(binding, config), ())

def certification_is_current(
    result: CertificationResult,
    binding: Mapping[str, object],
    config: Mapping[str, object],
) -> bool:
    """A pass authorizes only the exact inputs that produced its fingerprint."""
    if not result.certified or result.fingerprint is None:
        return False
    blockers: list[str] = []
    _thresholds(config, blockers)
    if blockers or not binding:
        return False
    return result.fingerprint == _fingerprint(binding, config)


def _thresholds(config: Mapping[str, object], blockers: list[str]) -> dict[str, object] | None:
    if not isinstance(config, Mapping):
        blockers.append("certification configuration is incomplete")
        return None
    for name in _REQUIRED_CONFIG:
        if name not in config or config[name] is None:
            blockers.append(f"{name} is not explicitly configured")
    values: dict[str, object] = {}
    for name in _REQUIRED_CONFIG[:6]:
        if name not in config:
            continue
        try:
            value = Decimal(str(config[name]))
            if not value.is_finite():
                raise InvalidOperation
            values[name] = value
        except (InvalidOperation, ValueError, TypeError):
            blockers.append(f"{name} must be a finite number")
    count = config.get("minimum_sample_count")
    if type(count) is not int or count < 1:
        blockers.append("minimum_sample_count must be a positive integer")
    else:
        values["minimum_sample_count"] = count
    multiplier = config.get("cost_slippage_stress_multiplier")
    try:
        stress = Decimal(str(multiplier))
        if not stress.is_finite() or stress != Decimal("1.5"):
            raise InvalidOperation
        values["cost_slippage_stress_multiplier"] = stress
    except (InvalidOperation, ValueError, TypeError):
        blockers.append("cost_slippage_stress_multiplier must explicitly equal 1.5")

    walk = config.get("walk_forward")
    if not isinstance(walk, Mapping):
        blockers.append("walk_forward must explicitly configure split counts")
    else:
        for name in ("splits", "minimum_passing_splits"):
            if type(walk.get(name)) is not int or walk[name] < 1:
                blockers.append(f"walk_forward.{name} must be a positive integer")
        if (
            type(walk.get("splits")) is int
            and type(walk.get("minimum_passing_splits")) is int
            and walk["minimum_passing_splits"] > walk["splits"]
        ):
            blockers.append("walk_forward.minimum_passing_splits exceeds splits")
        else:
            values["walk_forward"] = dict(walk)

    perturbation = config.get("neighbor_parameter_perturbation")
    if not isinstance(perturbation, Mapping) or not perturbation:
        blockers.append("neighbor_parameter_perturbation must explicitly configure ranges")
    else:
        for name, amount in perturbation.items():
            try:
                value = Decimal(str(amount))
                if not name or not value.is_finite() or value <= 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError, TypeError):
                blockers.append(f"neighbor_parameter_perturbation.{name} must be positive")
        values["neighbor_parameter_perturbation"] = dict(perturbation)
    if "profit_factor_minimum" in values and values["profit_factor_minimum"] < Decimal("1.25"):
        blockers.append("profit_factor_minimum must be at least 1.25")
    for name in ("in_sample_profit_factor_minimum", "out_of_sample_profit_factor_minimum"):
        if name in values and values[name] < Decimal(1):
            blockers.append(f"{name} must be at least 1")
    if any(key.startswith("walk_forward.") for key in blockers):
        values.pop("walk_forward", None)
    return None if blockers else values


def _evaluate(evidence: CertificationEvidence, thresholds: dict[str, object], blockers: list[str]) -> None:
    def metric(value: Decimal | None, name: str) -> Decimal | None:
        if not isinstance(value, Decimal) or not value.is_finite():
            blockers.append(f"{name} is missing or invalid")
            return None
        return value

    net_return = metric(evidence.net_return, "net_return")
    if net_return is not None and (net_return <= 0 or net_return < thresholds["net_return_minimum"]):
        blockers.append("net_return")

    profits = evidence.paired_trade_net_profits
    if not profits or any(
        not isinstance(value, Decimal) or not value.is_finite() for value in profits
    ):
        blockers.append("paired_trade_net_profits are missing or incomplete")
    else:
        average = sum(profits, Decimal(0)) / len(profits)
        if average <= 0 or average < thresholds["paired_trade_average_net_profit_minimum"]:
            blockers.append("paired_trade_average_net_profit")

    pf = metric(evidence.profit_factor, "profit_factor")
    if pf is not None and pf < max(Decimal("1.25"), thresholds["profit_factor_minimum"]):
        blockers.append("profit_factor")
    for name, value in (
        ("in_sample_profit_factor", evidence.in_sample_profit_factor),
        ("out_of_sample_profit_factor", evidence.out_of_sample_profit_factor),
    ):
        observed = metric(value, name)
        if observed is not None and observed <= max(Decimal(1), thresholds[name + "_minimum"]):
            blockers.append(name)

    drawdown = metric(evidence.maximum_drawdown, "maximum_drawdown")
    if drawdown is not None and (drawdown < 0 or drawdown > thresholds["maximum_drawdown"]):
        blockers.append("maximum_drawdown")
    if type(evidence.sample_count) is not int:
        blockers.append("sample_count is missing")
    elif evidence.sample_count < thresholds["minimum_sample_count"]:
        blockers.append("sample_count")

    stress_return = metric(evidence.stressed_net_return, "stressed_net_return")
    stress_pf = metric(evidence.stressed_profit_factor, "stressed_profit_factor")
    stress_multiplier = metric(
        evidence.stressed_cost_slippage_multiplier,
        "stressed_cost_slippage_multiplier",
    )
    if stress_multiplier is not None and stress_multiplier != Decimal("1.5"):
        blockers.append("cost_slippage_stress.multiplier")
    if stress_return is not None and (
        stress_return <= 0 or stress_return < thresholds["net_return_minimum"]
    ):
        blockers.append("cost_slippage_stress.net_return")
    if stress_pf is not None and stress_pf < max(Decimal("1.25"), thresholds["profit_factor_minimum"]):
        blockers.append("cost_slippage_stress.profit_factor")

    neighbor_count = len(thresholds["neighbor_parameter_perturbation"])
    if len(evidence.neighbor_net_returns) != neighbor_count or len(evidence.neighbor_profit_factors) != neighbor_count:
        blockers.append("neighbor_parameter_metrics are incomplete")
    else:
        for index, (neighbor_return, neighbor_pf) in enumerate(
            zip(evidence.neighbor_net_returns, evidence.neighbor_profit_factors)
        ):
            if (
                not isinstance(neighbor_return, Decimal)
                or not neighbor_return.is_finite()
                or neighbor_return <= 0
                or neighbor_return < thresholds["net_return_minimum"]
            ):
                blockers.append(f"neighbor_parameter_metrics[{index}].net_return")
            if (
                not isinstance(neighbor_pf, Decimal)
                or not neighbor_pf.is_finite()
                or neighbor_pf < max(Decimal("1.25"), thresholds["profit_factor_minimum"])
            ):
                blockers.append(f"neighbor_parameter_metrics[{index}].profit_factor")
    walk = thresholds["walk_forward"]
    expected_splits = walk["splits"]
    if len(evidence.walk_forward_net_returns) != expected_splits or len(evidence.walk_forward_profit_factors) != expected_splits:
        blockers.append("walk_forward_metrics are incomplete")
    else:
        passing = 0
        for index, (window_return, window_pf) in enumerate(
            zip(evidence.walk_forward_net_returns, evidence.walk_forward_profit_factors)
        ):
            if (
                not isinstance(window_return, Decimal)
                or not isinstance(window_pf, Decimal)
                or not window_return.is_finite()
                or not window_pf.is_finite()
            ):
                blockers.append(f"walk_forward_metrics[{index}] are invalid")
            elif window_return > 0 and window_pf > 1:
                passing += 1
        if passing < walk["minimum_passing_splits"]:
            blockers.append("walk_forward")


def _fingerprint(binding: Mapping[str, object], config: Mapping[str, object]) -> str:
    payload = {"binding": binding, "certification_config": config}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
