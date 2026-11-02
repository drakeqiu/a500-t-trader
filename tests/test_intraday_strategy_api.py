from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from a500_t_trader.backtest.certification import (
    CertificationEvidence,
    CertificationResult,
    certify_intraday_backtest,
)
from a500_t_trader.market_data import MinuteBar
from a500_t_trader.strategy.intraday_api import evaluate_intraday_strategy
from a500_t_trader.strategy.intraday_state import IntradayStrategyStateStore
from a500_t_trader.strategy.intraday_technical import RULE_SOURCE_SHA256, TECHNICAL_PARAMETERS
from a500_t_trader.strategy.intraday_v1_config import IntradayV1Config

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 11, 2, 10, 0, tzinfo=SHANGHAI)


def _bars(timestamp: datetime = AS_OF - timedelta(minutes=5)) -> tuple[MinuteBar, ...]:
    start = datetime(2026, 10, 30, 9, 30, tzinfo=SHANGHAI)
    rows = []
    for index in range(179):
        close = Decimal("4") + Decimal(index) / Decimal("10000")
        rows.append(
            MinuteBar(
                "510300",
                start + timedelta(minutes=5 * index),
                5,
                close,
                close + Decimal("0.01"),
                close - Decimal("0.01"),
                close,
                10000,
                close * 10000,
                True,
            )
        )
    close = Decimal("4.02")
    rows.append(
        MinuteBar(
            "510300",
            timestamp,
            5,
            close,
            close + Decimal("0.01"),
            close - Decimal("0.01"),
            close,
            10000,
            close * 10000,
            True,
        )
    )
    return tuple(rows)


def _config(
    account_as_of: datetime = AS_OF,
    rule_as_of: datetime = AS_OF,
    *,
    positions: dict[str, dict[str, object]] | None = None,
    securities: dict[str, dict[str, object]] | None = None,
) -> IntradayV1Config:
    position_data = positions if positions is not None else {
        "510300": {
            "quantity": 1000,
            "protected_floor": 300,
            "cost_basis": "3.8",
            "sellable_quantity": 700,
            "market_value": "4000",
        }
    }
    security_data = securities if securities is not None else {
        "510300": {
            "t_plus": "T+1",
            "lot_size": 100,
            "tick_size": "0.001",
            "lower_price_limit": "3.6",
            "upper_price_limit": "4.4",
            "source": "exchange rule sheet",
            "as_of": rule_as_of.isoformat(),
        }
    }
    parameters = {
        **TECHNICAL_PARAMETERS,
        "grid": {
            **TECHNICAL_PARAMETERS["grid"],
            "grid_version": 1,
            "grid_layer": 0,
            "grid_round": 0,
        },
        "signals": {"buy_threshold": 72, "sell_threshold": 68},
        "risk": {
            "max_daily_trades": 4,
            "max_daily_adds": 2,
            "max_daily_turnover": "100000",
            "min_same_direction_bars": 0,
            "portfolio_limit": "0.75",
        },
        "liquidity": {"minimum_amount": "0"},
    }
    return IntradayV1Config(
        "intraday-etf-t-v1",
        "V1.0",
        "commit:test",
        "docs/strategy/intraday-etf-t-v1/场内ETF日内做T量化策略_V1.0.txt",
        RULE_SOURCE_SHA256,
        parameters,
        {
            "net_return_minimum": "0",
            "paired_trade_average_net_profit_minimum": "0",
            "profit_factor_minimum": "1.25",
            "in_sample_profit_factor_minimum": "1",
            "out_of_sample_profit_factor_minimum": "1",
            "maximum_drawdown": "0.10",
            "minimum_sample_count": 20,
            "cost_slippage_stress_multiplier": "1.5",
            "walk_forward": {"splits": 3, "minimum_passing_splits": 3},
            "neighbor_parameter_perturbation": {"grid.width": "0.001"},
        },
        {
            "commission_rate": "0.0003",
            "minimum_commission": "5",
            "impact_bps": "2",
            "source": "user supplied",
            "as_of": AS_OF.isoformat(),
        },
        {
            "as_of": account_as_of.isoformat(),
            "cash": "50000",
            "positions": position_data,
        },
        security_data,
        "a" * 64,
    )

def _evaluate(**overrides: object) -> dict[str, dict[str, object]]:
    arguments: dict[str, object] = {
        "bars_by_symbol": {"510300": _bars()},
        "as_of": AS_OF,
        "config": _config(),
        "certification": None,
        "certification_binding": None,
    }
    arguments.update(overrides)
    return evaluate_intraday_strategy(**arguments)  # type: ignore[arg-type]
def _current_binding(config: IntradayV1Config) -> dict[str, object]:
    return {
        "rule_source_sha256": config.rule_source_sha256,
        "rule_version": config.rule_version,
        "parameters": config.parameters,
        "implementation_version": config.implementation_version,
        "data_snapshot_sha256": "b" * 64,
        "cost_model": config.fees,
    }


def test_technical_candidate_uses_exact_config_and_reports_missing_grid_context() -> None:
    result = _evaluate()["510300"]

    signal = result["signal"]
    assert isinstance(signal, dict)
    assert signal["candidate"] == "WAIT"
    assert set(signal["scores"]) == {"buy", "sell", "delta"}
    assert "dynamic grid inputs unavailable or grid width exceeds 0.90%" in signal["reasons"]
    assert result["risk"]["status"] == "BLOCKED"  # type: ignore[index]


def test_certification_gate_reports_missing_failed_and_mismatched_binding() -> None:
    config = _config()
    evidence = CertificationResult(False, None, ("failed",), {})
    passed_but_unverifiable = CertificationResult(True, "f" * 64, (), {})
    cases = (
        (_evaluate()["510300"], "certification evidence unavailable"),
        (
            _evaluate(certification=evidence)["510300"],
            "certification is failed or not passed",
        ),
        (
            _evaluate(
                certification=passed_but_unverifiable,
                certification_binding=_current_binding(config),
            )["510300"],
            "certification is stale or mismatched with current inputs",
        ),
        (
            _evaluate(
                certification=passed_but_unverifiable,
                certification_binding=_current_binding(config) | {"rule_version": "V0"},
            )["510300"],
            "certification binding mismatches current configuration: rule_version",
        ),
    )
    for result, reason in cases:
        _assert_blocked(result, reason)


def test_current_certification_binding_is_accepted() -> None:
    config = _config()
    binding = _current_binding(config)
    evidence = CertificationEvidence(
        Decimal("1"),
        tuple(Decimal("1") for _ in range(20)),
        Decimal("2"),
        Decimal("2"),
        Decimal("2"),
        Decimal("0"),
        20,
        Decimal("0.5"),
        Decimal("1.5"),
        Decimal("1.5"),
        (Decimal("1"),),
        (Decimal("1.5"),),
        (Decimal("1"), Decimal("1"), Decimal("1")),
        (Decimal("1.5"), Decimal("1.5"), Decimal("1.5")),
    )
    certification = certify_intraday_backtest(evidence, binding, config.certification)
    assert certification.certified

    result = _evaluate(certification=certification, certification_binding=binding)["510300"]

    assert "certification is stale or mismatched with current inputs" not in result["reasons"]
    assert "certification binding unavailable" not in result["reasons"]


def _assert_blocked(result: dict[str, object], *reasons: str) -> None:
    execution = result["execution"]
    assert isinstance(execution, dict)
    assert execution["allowed"] is False
    assert execution["quantity"] in (None, 0)
    assert all(reason in result["reasons"] for reason in reasons)


def test_api_returns_separate_technical_candidate_and_risk_veto() -> None:
    result = _evaluate()["510300"]

    assert set(result) == {"signal", "position", "execution", "risk", "reasons"}
    signal = result["signal"]
    assert isinstance(signal, dict)
    assert signal["candidate"] == "WAIT"
    assert set(signal["scores"]) == {"buy", "sell", "delta"}
    assert signal["trend"] is not None
    assert result["risk"]["status"] == "BLOCKED"  # type: ignore[index]
    _assert_blocked(result, "certification evidence unavailable")


def test_each_missing_or_stale_upstream_input_blocks_action_with_specific_reason() -> None:
    stale = AS_OF - timedelta(hours=1)
    cases = (
        (
            _evaluate(config=None)["510300"],
            "account snapshot and V1 configuration unavailable",
        ),
        (
            _evaluate(config=_config(account_as_of=stale))["510300"],
            "account snapshot is stale or future-dated",
        ),
        (
            _evaluate(bars_by_symbol={"510300": ()})["510300"],
            "market data unavailable for 510300",
        ),
        (
            _evaluate(bars_by_symbol={"510300": _bars(stale)})["510300"],
            "market data for 510300 is stale or future-dated",
        ),
        (
            _evaluate(config=_config(securities={}))["510300"],
            "trading rules unavailable for 510300",
        ),
        (
            _evaluate(config=_config(rule_as_of=stale))["510300"],
            "trading rules for 510300 are stale or future-dated",
        ),
    )
    for result, reason in cases:
        _assert_blocked(result, reason)


def test_certification_mapping_is_not_accepted_as_verified_evidence() -> None:
    _assert_blocked(
        _evaluate(certification={"status": "passed"})["510300"],  # type: ignore[arg-type]
        "certification is failed or not passed",
    )


def test_api_is_deterministic_and_does_not_turn_unknowns_into_quantity() -> None:
    first = _evaluate()["510300"]
    second = _evaluate()["510300"]

    assert first == second
    _assert_blocked(first, "certification evidence unavailable")
    execution = first["execution"]
    assert isinstance(execution, dict)
    assert execution["suggested_price"] is None
    assert execution["order_type"] is None

def test_evaluation_requires_existing_persistent_state(tmp_path) -> None:
    store = IntradayStrategyStateStore(tmp_path / "strategy.sqlite")
    result = _evaluate(state_store=store)["510300"]

    _assert_blocked(result, "dynamic grid inputs unavailable or grid width exceeds 0.90%")
    assert store.state("510300", AS_OF.date()) is None
    assert store.trade_pairs() == []
