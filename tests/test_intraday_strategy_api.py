from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

import a500_t_trader.strategy.intraday_api as intraday_api
from a500_t_trader.market_data import MinuteBar
from a500_t_trader.strategy.intraday_api import evaluate_intraday_strategy
from a500_t_trader.strategy.intraday_state import (
    InitialHoldings,
    IntradayStrategyStateStore,
    StrategyFill,
    StrategyRecommendation,
)
from a500_t_trader.strategy.intraday_technical import (
    TECHNICAL_PARAMETERS,
    IntradayTechnicalAnalysis,
)
from a500_t_trader.strategy.intraday_v1_config import (
    RULE_SOURCE_SHA256,
    IntradayV1Config,
)

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
    position_data = (
        positions
        if positions is not None
        else {
            "510300": {
                "quantity": 1000,
                "protected_floor": 300,
                "cost_basis": "3.8",
                "sellable_quantity": 700,
                "market_value": "4000",
            }
        }
    )
    security_data = (
        securities
        if securities is not None
        else {
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
    )
    technical_grid = TECHNICAL_PARAMETERS["grid"]
    assert isinstance(technical_grid, dict)
    parameters = {
        **TECHNICAL_PARAMETERS,
        "grid": {
            **technical_grid,
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
        "intraday-etf-t-v1-rsi",
        "V1.0",
        "commit:test",
        "docs/strategy/intraday-etf-t-v1/intraday_etf_t_quantitative_strategy_v1.0.md",
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
    }
    arguments.update(overrides)
    return evaluate_intraday_strategy(**arguments)  # type: ignore[arg-type]


def _seed_pair(
    store: IntradayStrategyStateStore,
    pair_id: str,
    *,
    symbol: str = "510300",
    first_side: str = "buy",
    close_pair: bool = False,
) -> None:
    created_at = datetime(2026, 11, 1, 9, 30, tzinfo=SHANGHAI)
    if first_side == "sell":
        store.record_initial_holdings(
            InitialHoldings(symbol, created_at, 300, Decimal("4.00"), 500, 200)
        )
    first_recommendation_id = f"{pair_id}-{first_side}-recommendation"
    store.record_recommendation(
        StrategyRecommendation(
            first_recommendation_id, symbol, first_side, created_at, 1
        )
    )
    assert store.confirm_fill(
        StrategyFill(
            f"{pair_id}-{first_side}-fill",
            first_recommendation_id,
            pair_id,
            first_side,
            100,
            Decimal("4.00"),
            created_at,
        ),
        confirmed=True,
        simulated=False,
        order_state="filled",
        cooldown_minutes=0,
    )
    if close_pair:
        second_side = "sell" if first_side == "buy" else "buy"
        recommendation_id = f"{pair_id}-{second_side}-recommendation"
        store.record_recommendation(
            StrategyRecommendation(
                recommendation_id, symbol, second_side, created_at, 1
            )
        )
        assert store.confirm_fill(
            StrategyFill(
                f"{pair_id}-{second_side}-fill",
                recommendation_id,
                pair_id,
                second_side,
                100,
                Decimal("4.10"),
                created_at + timedelta(minutes=5),
            ),
            confirmed=True,
            simulated=False,
            order_state="filled",
            cooldown_minutes=0,
        )


def _candidate_analysis(
    candidate: str,
    current_rsi: Decimal | None = Decimal("35"),
    previous_rsi: Decimal | None = Decimal("34"),
) -> IntradayTechnicalAnalysis:
    return IntradayTechnicalAnalysis(
        symbol="510300",
        trend="RANGE",
        indicators={
            "close": Decimal("4.00"),
            "rsi_14": current_rsi,
            "rsi_14_previous": previous_rsi,
        },
        scores={"buy": 80.0, "sell": 80.0, "delta": 20.0},
        candidate=candidate,
        risk_status="READY",
        reasons=(),
        completed_valid_bars=180,
    )


@pytest.mark.parametrize(
    ("candidate", "current_rsi", "previous_rsi", "expected_reason"),
    (
        ("BUY", None, Decimal("29"), "RSI_NOT_READY"),
        ("BUY", Decimal("30"), Decimal("29"), "RSI_BUY_MOMENTUM_NOT_CONFIRMED"),
        ("BUY", Decimal("35"), Decimal("35"), "RSI_BUY_MOMENTUM_NOT_CONFIRMED"),
        ("BUYBACK", Decimal("25"), Decimal("22"), "RSI_BUY_MOMENTUM_NOT_CONFIRMED"),
        ("SELL", Decimal("70"), Decimal("71"), "RSI_SELL_MOMENTUM_NOT_CONFIRMED"),
        ("SELL", Decimal("65"), Decimal("65"), "RSI_SELL_MOMENTUM_NOT_CONFIRMED"),
        ("SELL", None, Decimal("71"), "RSI_NOT_READY"),
    ),
)
def test_public_evaluation_blocks_ordinary_candidates_without_rsi_momentum(
    monkeypatch: pytest.MonkeyPatch,
    candidate: str,
    current_rsi: Decimal | None,
    previous_rsi: Decimal | None,
    expected_reason: str,
) -> None:
    analysis = _candidate_analysis(candidate, current_rsi, previous_rsi)
    monkeypatch.setattr(
        intraday_api,
        "analyze_intraday_technical",
        lambda bars_by_symbol, **kwargs: {"510300": analysis},
    )

    result = _evaluate()["510300"]

    signal = result["signal"]
    assert isinstance(signal, dict)
    assert isinstance(signal["reasons"], list)
    assert isinstance(result["reasons"], list)
    assert signal["candidate"] == "WAIT"
    assert expected_reason in signal["reasons"]
    assert expected_reason in result["reasons"]
    execution = result["execution"]
    assert isinstance(execution, dict)
    assert execution["allowed"] is False
    assert execution["quantity"] == 0


@pytest.mark.parametrize(
    ("candidate", "current_rsi", "previous_rsi"),
    (
        ("BUY", Decimal("31"), Decimal("30")),
        ("BUYBACK", Decimal("31"), Decimal("30")),
        ("SELL", Decimal("69"), Decimal("70")),
    ),
)
def test_public_evaluation_preserves_rsi_confirmed_candidates(
    monkeypatch: pytest.MonkeyPatch,
    candidate: str,
    current_rsi: Decimal,
    previous_rsi: Decimal,
) -> None:
    analysis = _candidate_analysis(candidate, current_rsi, previous_rsi)
    monkeypatch.setattr(
        intraday_api,
        "analyze_intraday_technical",
        lambda bars_by_symbol, **kwargs: {"510300": analysis},
    )

    result = _evaluate()["510300"]

    signal = result["signal"]
    assert isinstance(signal, dict)
    assert signal["candidate"] == candidate


def test_public_evaluation_does_not_rsi_block_take_profit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis = _candidate_analysis("TAKE_PROFIT", None, None)
    monkeypatch.setattr(
        intraday_api,
        "analyze_intraday_technical",
        lambda bars_by_symbol, **kwargs: {"510300": analysis},
    )

    result = _evaluate()["510300"]

    signal = result["signal"]
    assert isinstance(signal, dict)
    assert signal["candidate"] == "TAKE_PROFIT"
    assert signal["scores"] == analysis.scores
    indicators = signal["indicators"]
    assert isinstance(indicators, dict)
    assert isinstance(result["reasons"], list)
    assert indicators["rsi_14"] is None
    assert indicators["rsi_14_previous"] is None
    assert not any(reason.startswith("RSI_") for reason in result["reasons"])


def test_public_evaluation_routes_explicit_risk_exit_around_rsi_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis = _candidate_analysis("SELL", None, None)
    monkeypatch.setattr(
        intraday_api,
        "analyze_intraday_technical",
        lambda bars_by_symbol, **kwargs: {"510300": analysis},
    )

    result = _evaluate(risk_exit_required={"510300": True})["510300"]

    signal = result["signal"]
    assert isinstance(signal, dict)
    assert signal["candidate"] == "FORCED_RISK_EXIT"
    assert isinstance(signal["reasons"], list)
    assert isinstance(result["reasons"], list)
    execution = result["execution"]
    assert isinstance(execution, dict)
    assert "RSI_NOT_READY" in signal["reasons"]
    assert "RSI_NOT_READY" not in result["reasons"]
    assert execution["quantity"] == 0
    assert execution["allowed"] is False


def _paired_api_result(
    monkeypatch: pytest.MonkeyPatch,
    store: IntradayStrategyStateStore,
    candidate: str,
    pair_id: str | None,
) -> dict[str, object]:
    analysis = _candidate_analysis(candidate)
    monkeypatch.setattr(
        intraday_api,
        "analyze_intraday_technical",
        lambda bars_by_symbol, **kwargs: {"510300": analysis},
    )
    pair_type = "take_profit" if candidate == "TAKE_PROFIT" else "buyback"
    result = _evaluate(
        state_store=store,
        paired_state={
            "510300": {
                pair_type: {
                    "target_price": "4.00",
                    "quantity": 100,
                    **({"trade_pair_id": pair_id} if pair_id is not None else {}),
                }
            }
        },
    )["510300"]
    return result


def test_technical_candidate_uses_exact_config_and_reports_missing_grid_context() -> (
    None
):
    result = _evaluate()["510300"]

    signal = result["signal"]
    assert isinstance(signal, dict)
    assert signal["candidate"] == "WAIT"
    assert set(signal["scores"]) == {"buy", "sell", "delta"}
    assert (
        "dynamic grid inputs unavailable or grid width exceeds 0.90%"
        in signal["reasons"]
    )
    assert result["risk"]["status"] == "BLOCKED"  # type: ignore[index]


def test_technical_intent_is_visible_without_actionable_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        intraday_api,
        "analyze_intraday_technical",
        lambda *args, **kwargs: {"510300": _candidate_analysis("BUY")},
    )

    result = _evaluate()["510300"]

    signal = result["signal"]
    execution = result["execution"]
    assert isinstance(signal, dict)
    assert signal["candidate"] == "BUY"
    assert isinstance(result["reasons"], list)
    assert isinstance(execution, dict)
    assert execution["allowed"] is False
    assert execution["quantity"] == 0
    assert not any("certification" in reason for reason in result["reasons"])


def _assert_blocked(result: dict[str, object], *reasons_to_check: str) -> None:
    execution = result["execution"]
    assert isinstance(execution, dict)
    assert execution["allowed"] is False
    assert execution["quantity"] in (None, 0)
    reasons = result["reasons"]
    assert isinstance(reasons, list)
    assert all(reason in reasons for reason in reasons_to_check)


def test_api_returns_separate_technical_candidate_and_risk_veto() -> None:
    result = _evaluate()["510300"]

    assert set(result) == {"signal", "position", "execution", "risk", "reasons"}
    signal = result["signal"]
    assert isinstance(signal, dict)
    assert signal["candidate"] == "WAIT"
    assert set(signal["scores"]) == {"buy", "sell", "delta"}
    assert signal["trend"] is not None
    assert result["risk"]["status"] == "BLOCKED"  # type: ignore[index]
    _assert_blocked(result)


def test_each_missing_or_stale_upstream_input_blocks_action_with_specific_reason() -> (
    None
):
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


def test_api_is_deterministic_and_does_not_turn_unknowns_into_quantity() -> None:
    first = _evaluate()["510300"]
    second = _evaluate()["510300"]

    assert first == second
    reasons = first["reasons"]
    assert isinstance(reasons, list)
    assert not any("certification" in reason for reason in reasons)
    execution = first["execution"]
    assert isinstance(execution, dict)
    assert execution["suggested_price"] is None
    assert execution["order_type"] is None


def test_paired_targets_do_not_bypass_source_candidate_rules() -> None:
    result = _evaluate(
        paired_state={
            "510300": {
                "take_profit": {
                    "target_price": "4.00",
                    "quantity": 700,
                    "trade_pair_id": "pair-tp",
                },
                "buyback": {
                    "target_price": "4.00",
                    "quantity": 700,
                    "trade_pair_id": "pair-bb",
                },
            }
        }
    )["510300"]

    signal = result["signal"]
    execution = result["execution"]
    assert isinstance(signal, dict)
    assert isinstance(execution, dict)
    assert signal["candidate"] == "WAIT"
    assert execution["allowed"] is False
    assert execution["quantity"] == 0


@pytest.mark.parametrize(
    ("candidate", "first_side"),
    (("TAKE_PROFIT", "buy"), ("BUYBACK", "sell")),
)
def test_valid_open_pair_id_is_returned_for_candidate(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch, candidate: str, first_side: str
) -> None:
    store = IntradayStrategyStateStore(postgres_url)
    pair_id = f"open-{candidate.lower()}"
    _seed_pair(store, pair_id, first_side=first_side)

    result = _paired_api_result(monkeypatch, store, candidate, pair_id)

    signal = result["signal"]
    assert isinstance(signal, dict)
    assert signal["candidate"] == candidate
    assert signal["trade_pair_id"] == pair_id


@pytest.mark.parametrize(
    ("candidate", "first_side", "symbol", "close_pair"),
    (
        ("TAKE_PROFIT", "sell", "510300", False),
        ("TAKE_PROFIT", "buy", "510500", False),
        ("TAKE_PROFIT", "buy", "510300", True),
        ("BUYBACK", "buy", "510300", False),
        ("BUYBACK", "sell", "510500", False),
        ("BUYBACK", "buy", "510300", True),
    ),
)
def test_invalid_pair_provenance_or_status_blocks_pair_id(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    candidate: str,
    first_side: str,
    symbol: str,
    close_pair: bool,
) -> None:
    store = IntradayStrategyStateStore(postgres_url)
    pair_id = f"invalid-{candidate.lower()}-{first_side}-{symbol}-{close_pair}"
    _seed_pair(
        store,
        pair_id,
        symbol=symbol,
        first_side=first_side,
        close_pair=close_pair,
    )

    result = _paired_api_result(monkeypatch, store, candidate, pair_id)

    signal = result["signal"]
    assert isinstance(signal, dict)
    assert signal["candidate"] == candidate
    assert "trade_pair_id" not in signal
    _assert_blocked(
        result,
        "paired intent does not match an open persisted pair with eligible fills",
    )


@pytest.mark.parametrize("pair_id", ("unknown-pair", None))
def test_missing_or_unknown_pair_id_fails_closed(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch, pair_id: str | None
) -> None:
    store = IntradayStrategyStateStore(postgres_url)

    result = _paired_api_result(monkeypatch, store, "TAKE_PROFIT", pair_id)

    signal = result["signal"]
    assert isinstance(signal, dict)
    assert "trade_pair_id" not in signal
    _assert_blocked(
        result,
        "paired intent does not match an open persisted pair with eligible fills",
    )


def test_evaluation_requires_existing_persistent_state(postgres_url: str) -> None:
    store = IntradayStrategyStateStore(postgres_url)
    result = _evaluate(state_store=store)["510300"]

    _assert_blocked(
        result, "dynamic grid inputs unavailable or grid width exceeds 0.90%"
    )
    assert store.state("510300", AS_OF.date()) is None
    assert store.trade_pairs() == []
