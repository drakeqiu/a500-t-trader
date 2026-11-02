import json
from pathlib import Path

import pytest

from a500_t_trader.strategy.intraday_v1_config import (
    IntradayV1Config,
    load_intraday_v1_config,
)


def _complete_config() -> dict[str, object]:
    return {
        "strategy_id": "intraday-etf-t-v1",
        "rule_version": "1.0.0",
        "implementation_version": "commit:abc123",
        "parameters": {
            "indicators": {"ema": [20, 60], "bollinger": [20, 2]},
            "trend": {"bull_d": "0.20", "bull_m": "0.05"},
            "scores": {"weights": {"ema": 20, "bollinger": 20}},
            "signals": {"buy_threshold": 72},
            "grid": {"anchor": "09:45", "maximum_width": "0.009"},
            "risk": {"portfolio_limit": "0.75"},
            "liquidity": {"minimum_amount": "30000000"},
            "timing": {"bar_interval_minutes": 5},
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
    assert config.strategy_id == "intraday-etf-t-v1"
    assert config.rule_version == "1.0.0"
    assert len(config.fingerprint) == 64
    assert config.securities["510300"]["t_plus"] == "T+1"


def test_missing_configuration_is_reported_by_exact_field_path(tmp_path: Path) -> None:
    document = _complete_config()
    del document["fees"]

    with pytest.raises(ValueError, match="missing required configuration: fees"):
        load_intraday_v1_config(_write_config(tmp_path / "rules.json", document))


def test_no_fee_example_or_security_category_inference_is_used(tmp_path: Path) -> None:
    document = _complete_config()
    del document["fees"]["minimum_commission"]  # type: ignore[index]
    del document["securities"]["510300"]["t_plus"]  # type: ignore[index]

    with pytest.raises(ValueError, match="fees.minimum_commission"):
        load_intraday_v1_config(_write_config(tmp_path / "rules.json", document))


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

    assert output["strategy_id"] == "intraday-etf-t-v1"
    assert output["rule_version"] == "1.0.0"
    assert len(output["configuration_fingerprint"]) == 64
