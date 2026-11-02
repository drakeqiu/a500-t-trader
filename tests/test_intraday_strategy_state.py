from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from a500_t_trader.strategy.intraday_state import (
    IntradayStrategyStateStore,
    InitialHoldings,
    StrategyFill,
    StrategyRecommendation,
    StrategyState,
)


NOW = datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc)


def test_strategy_constraints_survive_store_reopen(tmp_path):
    database = tmp_path / "strategy.sqlite"
    state = StrategyState(
        symbol="510300",
        session_date=date(2026, 11, 2),
        grid_version=3,
        grid_layer=2,
        grid_round=4,
        spacing=Decimal("0.0125"),
        same_direction_bars=1,
        cooldown_until=datetime(2026, 11, 2, 2, 10, tzinfo=timezone.utc),
        reset_anchor_crossings=1,
        daily_trade_count=5,
        daily_add_count=2,
        daily_turnover=Decimal("128000.00"),
    )
    IntradayStrategyStateStore(database).save_state(state)

    reopened = IntradayStrategyStateStore(database)

    assert reopened.state("510300", date(2026, 11, 2)) == state


def test_explicit_initial_holdings_do_not_create_strategy_pairs(tmp_path):
    store = IntradayStrategyStateStore(tmp_path / "strategy.sqlite")
    holdings = InitialHoldings(
        symbol="510300",
        as_of=NOW,
        protected_floor=1000,
        cost_basis=Decimal("3.85"),
        total_quantity=2500,
        sellable_quantity=2000,
    )

    store.record_initial_holdings(holdings)

    assert store.initial_holdings("510300") == holdings
    assert store.trade_pairs() == []


def test_only_confirmed_linked_actual_fills_change_pair_state(tmp_path):
    store = IntradayStrategyStateStore(tmp_path / "strategy.sqlite")
    recommendation = StrategyRecommendation(
        recommendation_id="rec-1",
        symbol="510300",
        side="buy",
        created_at=NOW,
        grid_version=3,
    )
    store.record_recommendation(recommendation)

    assert not store.confirm_fill(
        StrategyFill("sim-1", "rec-1", "pair-1", "buy", 100, Decimal("3.90"), NOW),
        confirmed=False,
        simulated=True,
    )
    assert store.trade_pairs() == []
    assert not store.confirm_fill(
        StrategyFill("manual-1", None, "pair-1", "buy", 100, Decimal("3.90"), NOW),
        confirmed=True,
        simulated=False,
    )
    assert store.trade_pairs() == []

    assert store.confirm_fill(
        StrategyFill("actual-buy", "rec-1", "pair-1", "buy", 100, Decimal("3.90"), NOW),
        confirmed=True,
        simulated=False,
    )
    assert store.trade_pairs() == [
        {
            "trade_pair_id": "pair-1",
            "symbol": "510300",
            "grid_version": 3,
            "buy_fill_id": "actual-buy",
            "sell_fill_id": None,
            "status": "open",
        }
    ]

    sell = StrategyRecommendation("rec-2", "510300", "sell", NOW, grid_version=4)
    store.record_recommendation(sell)
    assert store.confirm_fill(
        StrategyFill("actual-sell", "rec-2", "pair-1", "sell", 100, Decimal("3.95"), NOW),
        confirmed=True,
        simulated=False,
    )
    assert store.trade_pairs()[0]["status"] == "closed"
    assert store.trade_pairs()[0]["grid_version"] == 3


def test_recommendations_and_strategy_fills_do_not_change_account_snapshot(tmp_path):
    store = IntradayStrategyStateStore(tmp_path / "strategy.sqlite")
    holdings = InitialHoldings("510300", NOW, 1000, Decimal("3.85"), 2500, 2000)
    store.record_initial_holdings(holdings)
    store.record_recommendation(
        StrategyRecommendation("rec-1", "510300", "buy", NOW, grid_version=1)
    )
    store.confirm_fill(
        StrategyFill("actual-1", "rec-1", "pair-1", "buy", 100, Decimal("3.90"), NOW),
        confirmed=True,
        simulated=False,
    )

    assert store.initial_holdings("510300") == holdings


def test_invalid_fill_pairing_is_rejected(tmp_path):
    store = IntradayStrategyStateStore(tmp_path / "strategy.sqlite")
    store.record_recommendation(
        StrategyRecommendation("rec-1", "510300", "buy", NOW, grid_version=1)
    )
    store.confirm_fill(
        StrategyFill("buy-1", "rec-1", "pair-1", "buy", 100, Decimal("3.90"), NOW),
        confirmed=True,
        simulated=False,
    )

    with pytest.raises(ValueError, match="must match its recommendation"):
        store.confirm_fill(
            StrategyFill("sell-1", "rec-1", "pair-1", "sell", 100, Decimal("3.91"), NOW),
            confirmed=True,
            simulated=False,
        )


def test_grid_layer_round_use_is_durable_and_directional(tmp_path):
    path = tmp_path / "strategy.sqlite"
    store = IntradayStrategyStateStore(path)

    assert store.consume_grid_slot("510300", date(2026, 11, 2), 1, 2, 1, "buy")
    assert not IntradayStrategyStateStore(path).consume_grid_slot(
        "510300", date(2026, 11, 2), 1, 2, 1, "buy"
    )
    assert store.consume_grid_slot("510300", date(2026, 11, 2), 1, 2, 2, "buy")
    assert store.consume_grid_slot("510300", date(2026, 11, 2), 2, 2, 1, "buy")
    assert store.consume_grid_slot("510300", date(2026, 11, 2), 1, 2, 1, "sell")
