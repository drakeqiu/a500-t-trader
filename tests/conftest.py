import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql


@contextmanager
def _isolated_postgres_url() -> Iterator[str]:
    """Use a private schema on the explicitly configured PostgreSQL test server."""
    base_url = os.environ.get("A500_TEST_DATABASE_URL")
    if not base_url:
        pytest.fail("A500_TEST_DATABASE_URL must point to a PostgreSQL test database")
    parts = urlsplit(base_url)
    if parts.scheme not in ("postgres", "postgresql"):
        pytest.fail("A500_TEST_DATABASE_URL must be a PostgreSQL URL")
    schema = "a500_test_" + uuid.uuid4().hex
    params = parse_qsl(parts.query, keep_blank_values=True)
    options = [value for key, value in params if key == "options"]
    params = [(key, value) for key, value in params if key != "options"]
    params.append(("options", " ".join([*options, f"-csearch_path={schema}"])))
    scoped_url = urlunsplit(parts._replace(query=urlencode(params)))
    with psycopg.connect(base_url, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield scoped_url
    finally:
        with psycopg.connect(base_url, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def postgres_url() -> Iterator[str]:
    with _isolated_postgres_url() as url:
        yield url


@pytest.fixture
def postgres_url_other() -> Iterator[str]:
    with _isolated_postgres_url() as url:
        yield url
