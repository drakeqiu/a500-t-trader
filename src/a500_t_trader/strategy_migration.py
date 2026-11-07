"""PostgreSQL schema for intraday strategy persistence."""

import psycopg


def migrate(db: psycopg.Connection) -> None:
    """Create strategy tables in the caller's migration transaction."""
    db.execute("""CREATE TABLE IF NOT EXISTS trigger_evidence (
        symbol TEXT NOT NULL, direction TEXT NOT NULL, condition TEXT NOT NULL,
        bar_timestamp TEXT NOT NULL, bar_interval_minutes INTEGER NOT NULL,
        bar_open TEXT NOT NULL, bar_high TEXT NOT NULL, bar_low TEXT NOT NULL,
        bar_close TEXT NOT NULL, bar_volume BIGINT NOT NULL, bar_amount TEXT,
        bar_completed INTEGER NOT NULL, bar_source TEXT, session_state TEXT NOT NULL,
        evaluated_at TEXT NOT NULL, risk_or_invalidation TEXT NOT NULL,
        account_as_of TEXT NOT NULL, account_source TEXT NOT NULL,
        PRIMARY KEY (symbol, direction, bar_interval_minutes, bar_timestamp)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS intraday_strategy_state (
        symbol TEXT NOT NULL, session_date TEXT NOT NULL, grid_version INTEGER NOT NULL,
        grid_layer INTEGER NOT NULL, grid_round INTEGER NOT NULL, spacing TEXT NOT NULL,
        same_direction_bars INTEGER NOT NULL, cooldown_until TEXT,
        reset_anchor_crossings INTEGER NOT NULL, daily_trade_count INTEGER NOT NULL,
        daily_add_count INTEGER NOT NULL, daily_turnover TEXT NOT NULL,
        PRIMARY KEY(symbol, session_date)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS strategy_initial_holdings (
        symbol TEXT PRIMARY KEY, record_json TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS strategy_recommendations (
        recommendation_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('buy','sell')), created_at TEXT NOT NULL,
        grid_version INTEGER NOT NULL, grid_layer INTEGER NOT NULL,
        grid_round INTEGER NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS strategy_trade_pairs (
        trade_pair_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
        grid_version INTEGER NOT NULL,
        buy_fill_id TEXT, sell_fill_id TEXT, buy_quantity BIGINT NOT NULL,
        sell_quantity BIGINT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('open','closed'))
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS strategy_actual_fills (
        fill_id TEXT PRIMARY KEY,
        recommendation_id TEXT NOT NULL
            REFERENCES strategy_recommendations(recommendation_id),
        trade_pair_id TEXT NOT NULL REFERENCES strategy_trade_pairs(trade_pair_id),
        side TEXT NOT NULL CHECK(side IN ('buy','sell')),
        quantity BIGINT NOT NULL CHECK(quantity > 0), price TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        fill_sequence BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS consumed_grid_slots (
        symbol TEXT NOT NULL, session_date TEXT NOT NULL, grid_version INTEGER NOT NULL,
        grid_layer INTEGER NOT NULL, grid_round INTEGER NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('buy','sell')),
        PRIMARY KEY(symbol,session_date,grid_version,grid_layer,grid_round,side)
    )""")
