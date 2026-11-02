from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from a500_t_trader.market_data import MinuteBar
from a500_t_trader.strategy.intraday_api import evaluate_intraday_strategy
from a500_t_trader.strategy.intraday_state import IntradayStrategyStateStore
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
            "source": "exchange rule sheet",
            "as_of": rule_as_of.isoformat(),
        }
    }
    return IntradayV1Config(
        "intraday-etf-t-v1",
        "1.0.0",
        "commit:test",
        "source unavailable: V1 scoring components",
        "a" * 64,
        {
            "indicators": {"ema": [20, 60]},
            "trend": {"bull_d": "0.2"},
            "scores": {"weights": {"ema": 20}},
            "signals": {"buy_threshold": 72},
            "grid": {"anchor": "09:45"},
            "risk": {"portfolio_limit": "0.75"},
            "liquidity": {"minimum_amount": "30000000"},
            "timing": {"bar_interval_minutes": 5, "max_input_age_seconds": 1800},
        },
        {},
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
    }
    arguments.update(overrides)
    return evaluate_intraday_strategy(**arguments)  # type: ignore[arg-type]


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
    assert signal["scores"] == {"buy": None, "sell": None}
    assert signal["trend"] is not None
    assert any("exact V1.0 scoring rules" in reason for reason in signal["reasons"])
    assert result["risk"]["status"] == "BLOCKED"  # type: ignore[index]
    _assert_blocked(
        result,
        "certification evidence unavailable; no verifiable certification gate is installed",
        "V1 strategy certification has not passed",
    )


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


def test_unverified_failed_and_stale_certification_each_fail_closed() -> None:
    for certification, expected in (
        (None, "certification evidence unavailable; no verifiable certification gate is installed"),
        ({"status": "failed"}, "certification is failed or not passed"),
        (
            {"status": "passed", "as_of": AS_OF - timedelta(hours=1)},
            "certification is stale or future-dated",
        ),
        (
            {"status": "passed", "as_of": AS_OF},
            "certification evidence is unverified or not bound to current inputs",
        ),
    ):
        _assert_blocked(_evaluate(certification=certification)["510300"], expected)


def test_api_is_deterministic_and_does_not_turn_unknowns_into_quantity() -> None:
    first = _evaluate()["510300"]
    second = _evaluate()["510300"]

    assert first == second
    _assert_blocked(first, "V1 strategy certification has not passed")
    execution = first["execution"]
    assert isinstance(execution, dict)
    assert execution["suggested_price"] is None
    assert execution["order_type"] is None

def test_evaluation_requires_explicit_persistent_state_configuration(tmp_path) -> None:
    store = IntradayStrategyStateStore(tmp_path / "strategy.sqlite")
    result = _evaluate(state_store=store)["510300"]

    _assert_blocked(result, "persistent strategy state unavailable or unconfigured")
    assert store.state("510300", AS_OF.date()) is None
    assert store.trade_pairs() == []
