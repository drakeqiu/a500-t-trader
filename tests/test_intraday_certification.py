from dataclasses import replace
from decimal import Decimal

from a500_t_trader.backtest import (
    CertificationEvidence,
    certify_intraday_backtest,
    certification_is_current,
)


def binding() -> dict[str, object]:
    return {
        "rule_source_sha256": "a" * 64,
        "rule_version": "V1.0",
        "parameters": {"grid": {"width": "0.3%"}},
        "implementation_version": "commit:abc123",
        "data_snapshot_sha256": "b" * 64,
        "cost_model": {"commission": "0.0003", "slippage_bps": "2"},
    }


def config() -> dict[str, object]:
    return {
        "net_return_minimum": "0.01",
        "paired_trade_average_net_profit_minimum": "1",
        "profit_factor_minimum": "1.25",
        "in_sample_profit_factor_minimum": "1.01",
        "out_of_sample_profit_factor_minimum": "1.01",
        "maximum_drawdown": "0.1",
        "minimum_sample_count": 20,
        "cost_slippage_stress_multiplier": "1.5",
        "walk_forward": {"splits": 3, "minimum_passing_splits": 3},
        "neighbor_parameter_perturbation": {"grid.width": "0.001"},
    }


def passing_evidence() -> CertificationEvidence:
    return CertificationEvidence(
        net_return=Decimal("0.05"),
        paired_trade_net_profits=(Decimal("2"), Decimal("3")),
        profit_factor=Decimal("1.5"),
        in_sample_profit_factor=Decimal("1.2"),
        out_of_sample_profit_factor=Decimal("1.1"),
        maximum_drawdown=Decimal("0.05"),
        sample_count=22,
        stressed_net_return=Decimal("0.02"),
        stressed_profit_factor=Decimal("1.3"),
        stressed_cost_slippage_multiplier=Decimal("1.5"),
        neighbor_net_returns=(Decimal("0.02"),),
        neighbor_profit_factors=(Decimal("1.3"),),
        walk_forward_net_returns=(Decimal("0.02"), Decimal("0.03"), Decimal("0.01")),
        walk_forward_profit_factors=(Decimal("1.1"), Decimal("1.2"), Decimal("1.05")),
    )


def test_certificate_binds_exact_rule_implementation_data_costs_and_config() -> None:
    result = certify_intraday_backtest(passing_evidence(), binding(), config())
    assert result.certified is True
    assert certification_is_current(result, binding(), config()) is True

    changed = binding() | {"implementation_version": "commit:def456"}
    assert certification_is_current(result, changed, config()) is False
    assert certification_is_current(result, binding(), config() | {"maximum_drawdown": "0.08"}) is False
    assert certification_is_current(
        result,
        binding() | {"data_snapshot_sha256": "c" * 64},
        config(),
    ) is False
    assert certification_is_current(
        result,
        binding() | {"cost_model": {"commission": "0.0004", "slippage_bps": "2"}},
        config(),
    ) is False
    assert certification_is_current(
        result,
        binding() | {"parameters": {"grid": {"width": "0.4%"}}},
        config(),
    ) is False
    assert certification_is_current(
        result,
        binding() | {"rule_source_sha256": "c" * 64},
        config(),
    ) is False


def test_incomplete_certification_configuration_never_certifies() -> None:
    incomplete = config() | {"walk_forward": {"splits": 3}}
    result = certify_intraday_backtest(passing_evidence(), binding(), incomplete)
    assert result.certified is False
    assert "walk_forward.minimum_passing_splits" in result.blockers


def test_failed_or_incomplete_metrics_never_certify() -> None:
    evidence = passing_evidence()
    failed = replace(evidence, profit_factor=Decimal("1.24"))
    result = certify_intraday_backtest(failed, binding(), config())
    assert result.certified is False
    assert "profit_factor" in result.blockers
    stressed_with_wrong_cost = replace(
        evidence, stressed_cost_slippage_multiplier=Decimal("1.49")
    )
    assert not certify_intraday_backtest(
        stressed_with_wrong_cost, binding(), config()
    ).certified

    incomplete = CertificationEvidence(
        net_return=None,
        paired_trade_net_profits=(),
        profit_factor=None,
        in_sample_profit_factor=None,
        out_of_sample_profit_factor=None,
        maximum_drawdown=None,
        sample_count=None,
        stressed_net_return=None,
        stressed_profit_factor=None,
        stressed_cost_slippage_multiplier=None,
        neighbor_net_returns=(),
        neighbor_profit_factors=(),
        walk_forward_net_returns=(),
        walk_forward_profit_factors=(),
    )
    result = certify_intraday_backtest(incomplete, binding(), config())
    assert result.certified is False
    assert result.fingerprint is None
