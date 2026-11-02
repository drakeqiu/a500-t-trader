from datetime import date, datetime, timedelta, timezone
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
        cooldown_minutes=10,
    )
    assert store.trade_pairs() == []
    assert not store.confirm_fill(
        StrategyFill("manual-1", None, "pair-1", "buy", 100, Decimal("3.90"), NOW),
        confirmed=True,
        simulated=False,
        cooldown_minutes=10,
    )
    assert store.trade_pairs() == []

    assert store.confirm_fill(
        StrategyFill("actual-buy", "rec-1", "pair-1", "buy", 100, Decimal("3.90"), NOW),
        confirmed=True,
        simulated=False,
        order_state="filled",
        cooldown_minutes=10,
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
        order_state="filled",
        cooldown_minutes=10,
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
        order_state="filled",
        cooldown_minutes=10,
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
        order_state="filled",
        cooldown_minutes=10,
    )

    with pytest.raises(ValueError, match="must match its recommendation"):
        store.confirm_fill(
            StrategyFill("sell-1", "rec-1", "pair-1", "sell", 100, Decimal("3.91"), NOW),
            confirmed=True,
            simulated=False,
            order_state="filled",
            cooldown_minutes=10,
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
def test_admission_reuses_persisted_grid_cooldown_and_daily_limits(tmp_path):
    database = tmp_path / "strategy.sqlite"
    store = IntradayStrategyStateStore(database)
    state = StrategyState(
        symbol="510300",
        session_date=date(2026, 11, 2),
        grid_version=3,
        grid_layer=2,
        grid_round=4,
        spacing=Decimal("0.01"),
        same_direction_bars=1,
        cooldown_until=datetime(2026, 11, 2, 2, 10, tzinfo=timezone.utc),
        reset_anchor_crossings=0,
        daily_trade_count=8,
        daily_add_count=3,
        daily_turnover=Decimal("150000"),
    )
    store.save_state(state)
    reopened = IntradayStrategyStateStore(database)

    assert reopened.check_constraints(
        symbol="510300",
        session_date=date(2026, 11, 2),
        side="buy",
        now=NOW,
        grid_version=3,
        grid_layer=2,
        grid_round=4,
        pending_order_state="none",
        max_daily_trades=8,
        max_daily_adds=3,
        max_daily_turnover=Decimal("150000"),
        min_same_direction_bars=2,
    ) == (
        "grid slot already used",
        "direction cooldown is active",
        "same-direction bar interval not met",
        "daily trade limit reached",
        "daily add limit reached",
        "daily turnover limit reached",
    )


def test_unknown_or_pending_order_status_fails_closed(tmp_path):
    store = IntradayStrategyStateStore(tmp_path / "strategy.sqlite")
    store.record_recommendation(
        StrategyRecommendation("rec-1", "510300", "buy", NOW, grid_version=1)
    )
    fill = StrategyFill("fill-1", "rec-1", "pair-1", "buy", 100, Decimal("3.90"), NOW)

    assert not store.confirm_fill(
        fill, confirmed=True, simulated=False, order_state="pending", cooldown_minutes=10
    )
    assert not store.confirm_fill(
        fill, confirmed=True, simulated=False, order_state="unknown", cooldown_minutes=10
    )
    assert store.trade_pairs() == []


def test_grid_slot_is_consumed_only_by_confirmed_fill(tmp_path):
    database = tmp_path / "strategy.sqlite"
    store = IntradayStrategyStateStore(database)
    store.save_state(
        StrategyState(
            "510300", NOW.date(), 2, 1, 3, Decimal("0.01"), 2, None, 0, 0, 0,
            Decimal("0"),
        )
    )
    recommendation = StrategyRecommendation(
        "rec-1", "510300", "buy", NOW, grid_version=2,
        grid_layer=1, grid_round=3,
    )
    store.record_recommendation(recommendation)

    assert store.check_constraints(
        symbol="510300",
        session_date=NOW.date(),
        side="buy",
        now=NOW,
        grid_version=2,
        grid_layer=1,
        grid_round=3,
        pending_order_state="none",
        max_daily_trades=8,
        max_daily_adds=3,
        max_daily_turnover=Decimal("1000000"),
        min_same_direction_bars=2,
    ) == ()

    assert store.check_constraints(
        symbol="510300",
        session_date=NOW.date(),
        side="buy",
        now=NOW,
        grid_version=2,
        grid_layer=1,
        grid_round=3,
        pending_order_state="open",
        max_daily_trades=8,
        max_daily_adds=3,
        max_daily_turnover=Decimal("1000000"),
        min_same_direction_bars=2,
    ) == ("order state unavailable or pending",)
    assert not store.confirm_fill(
        StrategyFill("sim-1", "rec-1", "pair-1", "buy", 100, Decimal("3.90"), NOW),
        confirmed=False,
        simulated=True,
        order_state="filled",
        cooldown_minutes=10,
    )
    assert IntradayStrategyStateStore(database).check_constraints(
        symbol="510300",
        session_date=NOW.date(),
        side="buy",
        now=NOW,
        grid_version=2,
        grid_layer=1,
        grid_round=3,
        pending_order_state="none",
        max_daily_trades=8,
        max_daily_adds=3,
        max_daily_turnover=Decimal("1000000"),
        min_same_direction_bars=2,
    ) == ()

    assert store.confirm_fill(
        StrategyFill("fill-1", "rec-1", "pair-1", "buy", 100, Decimal("3.90"), NOW),
        confirmed=True,
        simulated=False,
        order_state="filled",
        cooldown_minutes=10,
    )
    persisted = IntradayStrategyStateStore(database).state("510300", NOW.date())
    assert persisted is not None
    assert persisted.daily_trade_count == 1
    assert persisted.daily_add_count == 1
    assert persisted.daily_turnover == Decimal("390.00")
    assert persisted.same_direction_bars == 0
    assert persisted.cooldown_until == NOW + timedelta(minutes=10)
    assert IntradayStrategyStateStore(database).check_constraints(
        symbol="510300",
        session_date=NOW.date(),
        side="buy",
        now=NOW,
        grid_version=2,
        grid_layer=1,
        grid_round=3,
        pending_order_state="none",
        max_daily_trades=8,
        max_daily_adds=3,
        max_daily_turnover=Decimal("1000000"),
        min_same_direction_bars=2,
    ) == (
        "grid slot already used",
        "direction cooldown is active",
        "same-direction bar interval not met",
    )


def test_grid_reset_reuses_slots_only_under_new_grid_version(tmp_path):
    store = IntradayStrategyStateStore(tmp_path / "strategy.sqlite")
    store.save_state(
        StrategyState(
            "510300", NOW.date(), 1, 0, 0, Decimal("0.01"), 2, None, 0, 0, 0,
            Decimal("0"),
        )
    )
    assert store.consume_grid_slot("510300", NOW.date(), 1, 0, 0, "buy")
    store.save_state(
        StrategyState(
            "510300", NOW.date(), 2, 0, 0, Decimal("0.02"), 2, None, 1, 0, 0,
            Decimal("0"),
        )
    )

    assert store.check_constraints(
        symbol="510300",
        session_date=NOW.date(),
        side="buy",
        now=NOW,
        grid_version=2,
        grid_layer=0,
        grid_round=0,
        pending_order_state="none",
        max_daily_trades=8,
        max_daily_adds=3,
        max_daily_turnover=Decimal("1000000"),
        min_same_direction_bars=2,
    ) == ()
