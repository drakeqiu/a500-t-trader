"""PostgreSQL connection configuration and versioned schema migrations."""

from __future__ import annotations

import os
from urllib.parse import quote

import psycopg


def database_url(value: str | None = None) -> str:
    """Resolve the explicit connection string before the environment default."""
    url = value or os.environ.get("DATABASE_URL")
    if not url and os.environ.get("POSTGRES_HOST"):
        user = quote(os.environ.get("POSTGRES_USER", "a500"), safe="")
        password = quote(os.environ["POSTGRES_PASSWORD"], safe="")
        host = os.environ["POSTGRES_HOST"]
        name = quote(os.environ.get("POSTGRES_DB", "a500"), safe="")
        url = f"postgresql://{user}:{password}@{host}/{name}"
    if not url:
        raise ValueError(
            "PostgreSQL connection string required: set DATABASE_URL or --database"
        )
    if not url.startswith(("postgresql://", "postgres://")):
        raise ValueError("database must be a PostgreSQL connection URL")
    return url


def initialize_database(dsn: str) -> None:
    """Apply schema versions atomically, serialized across application processes."""
    from . import account_migration, backtest_migration, strategy_migration

    with psycopg.connect(database_url(dsn)) as db:
        db.execute("SELECT pg_advisory_xact_lock(498719505098)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS a500_schema_migrations "
            "(version integer PRIMARY KEY)"
        )
        applied = {
            row[0] for row in db.execute("SELECT version FROM a500_schema_migrations")
        }
        migrations = (
            (1, account_migration.migrate),
            (2, backtest_migration.migrate),
            (3, strategy_migration.migrate),
            (4, backtest_migration.migrate_daily_trading_date),
        )
        for version, migrate in migrations:
            if version not in applied:
                migrate(db)
                db.execute(
                    "INSERT INTO a500_schema_migrations (version) VALUES (%s)",
                    (version,),
                )
