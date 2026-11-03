import hashlib
import json
from pathlib import Path

import pytest

from a500_t_trader.strategy.intraday_v1_config import (
    IntradayV1Config,
    load_intraday_v1_config,
)


def _complete_config() -> dict[str, object]:
    return {
        "schema_version": "2.0",
        "strategy_id": "intraday-etf-t-v1-rsi",
        "rule_version": "V1.0",
        "implementation_version": "commit:abc123",
        "rule_source": (
            "docs/strategy/intraday-etf-t-v1/"
            "intraday_etf_t_quantitative_strategy_v1.0.md"
        ),
        "rule_source_sha256": (
            "ea086d76c039ee9483f0d58bb17eb658811567e98cfd99ee79af6da3d7781bf4"
        ),
        "parameters": {
            "indicators": {"ema": [20, 60], "bollinger": [20, 2]},
            "trend": {"bull_d": "0.20", "bull_m": "0.05"},
            "scores": {"weights": {"ema": 20, "bollinger": 20}},
            "signals": {"buy_threshold": 72},
            "grid": {"anchor": "09:45", "maximum_width": "0.009"},
            "risk": {
                "portfolio_limit": "0.75",
                "daily_turnover_limit": "150000",
                "daily_sell_limit": "5400",
                "max_daily_additions": "3",
                "max_daily_fills": "8",
            },
            "liquidity": {"minimum_amount": "30000000"},
            "timing": {"bar_interval_minutes": 5, "max_input_age_seconds": 7200},
        },
        "certification": {
            "net_return_minimum": "0",
            "paired_trade_average_net_profit_minimum": "0",
            "profit_factor_minimum": "1.25",
            "in_sample_profit_factor_minimum": "1",
            "out_of_sample_profit_factor_minimum": "1",
            "maximum_drawdown": "0.10",
            "minimum_sample_count": 100,
            "cost_slippage_stress_multiplier": "1.5",
            "walk_forward": {"folds": 4},
            "neighbor_parameter_perturbation": {"range": "0.1"},
        },
        "fees": {
            "commission_rate": "0.0003",
            "minimum_commission": "5",
            "impact_bps": "2",
            "source": "user-provided broker schedule",
            "as_of": "2026-11-02T09:00:00+08:00",
        },
        "account": {
            "as_of": "2026-11-02T09:00:00+08:00",
            "cash": "50000",
            "positions": {
                "510300": {
                    "quantity": 1000,
                    "protected_floor": 300,
                    "cost_basis": "3.8",
                    "sellable_quantity": 700,
                    "market_value": "4000",
                }
            },
        },
        "securities": {
            "510300": {
                "t_plus": "T+1",
                "lot_size": 100,
                "tick_size": "0.001",
                "lower_price_limit": "3.6",
                "upper_price_limit": "4.4",
                "source": "exchange rule sheet",
                "as_of": "2026-11-02T09:00:00+08:00",
            }
        },
    }


def _write_config(path: Path, config: dict[str, object]) -> Path:
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_loads_explicit_v1_configuration_with_auditable_fingerprint(
    tmp_path: Path,
) -> None:
    config = load_intraday_v1_config(
        _write_config(tmp_path / "rules.json", _complete_config())
    )

    assert isinstance(config, IntradayV1Config)
    assert config.schema_version == "2.0"
    assert config.strategy_id == "intraday-etf-t-v1-rsi"
    assert config.rule_version == "V1.0"
    assert config.rule_source == (
        "docs/strategy/intraday-etf-t-v1/intraday_etf_t_quantitative_strategy_v1.0.md"
    )
    assert (
        config.rule_source_sha256
        == "ea086d76c039ee9483f0d58bb17eb658811567e98cfd99ee79af6da3d7781bf4"
    )
    assert len(config.fingerprint) == 64
    assert config.securities["510300"]["t_plus"] == "T+1"


def test_missing_configuration_is_reported_by_exact_field_path(tmp_path: Path) -> None:
    document = _complete_config()
    del document["fees"]

    with pytest.raises(ValueError, match="missing required configuration: fees"):
        load_intraday_v1_config(_write_config(tmp_path / "rules.json", document))


def test_no_fee_example_or_security_category_inference_is_used(tmp_path: Path) -> None:
    document = _complete_config()
    fees = document["fees"]
    securities = document["securities"]
    assert isinstance(fees, dict)
    assert isinstance(securities, dict)
    security = securities["510300"]
    assert isinstance(security, dict)
    del fees["minimum_commission"]
    del security["t_plus"]

    with pytest.raises(ValueError, match="fees.minimum_commission"):
        load_intraday_v1_config(_write_config(tmp_path / "rules.json", document))


def test_security_price_bands_are_explicit_configuration(tmp_path: Path) -> None:
    document = _complete_config()
    del document["securities"]["510300"]["upper_price_limit"]  # type: ignore[index]

    with pytest.raises(ValueError, match="securities.510300.upper_price_limit"):
        load_intraday_v1_config(_write_config(tmp_path / "rules.json", document))


def test_v1_rule_source_is_versioned_with_its_auditable_sha256() -> None:
    source = (
        Path(__file__).parents[1]
        / "docs"
        / "strategy"
        / "intraday-etf-t-v1"
        / "intraday_etf_t_quantitative_strategy_v1.0.md"
    )

    assert (
        hashlib.sha256(source.read_bytes()).hexdigest()
        == "ea086d76c039ee9483f0d58bb17eb658811567e98cfd99ee79af6da3d7781bf4"
    )


def test_rule_source_must_have_verifiable_sha256(tmp_path: Path) -> None:
    document = _complete_config()
    document["rule_source_sha256"] = "unversioned"

    with pytest.raises(ValueError, match="rule_source_sha256"):
        load_intraday_v1_config(_write_config(tmp_path / "rules.json", document))


def test_legacy_schema_is_rejected_without_migration(tmp_path: Path) -> None:
    document = _complete_config()
    del document["schema_version"]

    with pytest.raises(ValueError, match="schema_version"):
        load_intraday_v1_config(_write_config(tmp_path / "legacy.json", document))


def test_unsupported_major_schema_is_rejected(tmp_path: Path) -> None:
    document = _complete_config()
    document["schema_version"] = "1.0"

    with pytest.raises(ValueError, match="unsupported schema_version"):
        load_intraday_v1_config(_write_config(tmp_path / "old-major.json", document))


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("strategy_id", "intraday-etf-t-v1", "strategy_id"),
        ("rule_source", "different-source.md", "rule_source"),
        ("rule_source_sha256", "0" * 64, "does not match"),
    ),
)
def test_ruleset_identity_mismatches_fail_closed(
    tmp_path: Path, field: str, value: str, reason: str
) -> None:
    document = _complete_config()
    document[field] = value

    with pytest.raises(ValueError, match=reason):
        load_intraday_v1_config(_write_config(tmp_path / "mismatch.json", document))


def test_fingerprint_changes_when_any_strategy_input_changes(tmp_path: Path) -> None:
    first = load_intraday_v1_config(
        _write_config(tmp_path / "first.json", _complete_config())
    )
    changed = _complete_config()
    changed["parameters"]["signals"]["buy_threshold"] = 73  # type: ignore[index]
    second = load_intraday_v1_config(_write_config(tmp_path / "second.json", changed))

    assert first.fingerprint != second.fingerprint


def test_cli_validates_and_reports_auditable_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from a500_t_trader.cli.main import main

    path = _write_config(tmp_path / "rules.json", _complete_config())
    assert main(["intraday-config", "--config", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["schema_version"] == "2.0"
    assert output["strategy_id"] == "intraday-etf-t-v1-rsi"
    assert output["rule_version"] == "V1.0"
    assert output["implementation_version"] == "commit:abc123"
    assert output["rule_source"] == (
        "docs/strategy/intraday-etf-t-v1/intraday_etf_t_quantitative_strategy_v1.0.md"
    )
    assert (
        output["rule_source_sha256"]
        == "ea086d76c039ee9483f0d58bb17eb658811567e98cfd99ee79af6da3d7781bf4"
    )
    assert len(output["configuration_fingerprint"]) == 64


def test_cli_evaluates_exports_api_result_and_reports_execution_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import datetime
    from decimal import Decimal

    from a500_t_trader.cli.main import main
    from a500_t_trader.market_data import MinuteBar
    from a500_t_trader.strategy.intraday_api import evaluate_intraday_strategy

    config_path = _write_config(tmp_path / "rules.json", _complete_config())
    bars_path = tmp_path / "bars.json"
    bars_path.write_text(
        json.dumps(
            [
                {
                    "symbol": "510300",
                    "timestamp": "2026-11-02T09:59:00+08:00",
                    "interval_minutes": 5,
                    "open": "4.02",
                    "high": "4.03",
                    "low": "4.01",
                    "close": "4.02",
                    "volume": 10000,
                }
            ]
        ),
        encoding="utf-8",
    )
    export_path = tmp_path / "result.json"
    assert (
        main(
            [
                "intraday-strategy",
                "--config",
                str(config_path),
                "--bars",
                str(bars_path),
                "--as-of",
                "2026-11-02T10:00:00+08:00",
                "--output",
                str(export_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    displayed = json.loads(captured.out)
    assert json.loads(export_path.read_text(encoding="utf-8")) == displayed
    api_result = evaluate_intraday_strategy(
        {
            "510300": (
                MinuteBar(
                    "510300",
                    datetime.fromisoformat("2026-11-02T09:59:00+08:00"),
                    5,
                    Decimal("4.02"),
                    Decimal("4.03"),
                    Decimal("4.01"),
                    Decimal("4.02"),
                    10000,
                ),
            )
        },
        as_of=datetime.fromisoformat("2026-11-02T10:00:00+08:00"),
        config=load_intraday_v1_config(config_path),
    )
    assert displayed == json.loads(json.dumps(api_result, default=str))
    execution = displayed["510300"]["execution"]
    assert execution["allowed"] is False
    assert execution["quantity"] in (None, 0)
    assert "does not connect to a broker" in captured.err
    assert "duplicate orders" in captured.err
