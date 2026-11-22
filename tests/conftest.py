import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    """Reuse configured PostgreSQL or start a temporary Podman instance."""
    base_url = os.environ.get("TEST_DATABASE_URL")
    if base_url:
        yield base_url
        return

    podman = shutil.which("podman")
    if podman is None:
        pytest.fail(
            "TEST_DATABASE_URL is unset and Podman is unavailable; "
            "install Podman or configure a PostgreSQL test database"
        )

    name = f"a500-pytest-{uuid.uuid4().hex[:12]}"
    result = subprocess.run(
        [
            podman,
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--env",
            "POSTGRES_DB=a500test",
            "--env",
            "POSTGRES_USER=postgres",
            "--env",
            "POSTGRES_PASSWORD=postgres",
            "--publish",
            "127.0.0.1::5432",
            "postgres:17-alpine",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.fail(f"Could not start temporary PostgreSQL: {result.stderr}")

    try:
        port_result = subprocess.run(
            [podman, "port", name, "5432/tcp"],
            capture_output=True,
            text=True,
            check=False,
        )
        if port_result.returncode:
            pytest.fail(
                f"Could not find temporary PostgreSQL port: "
                f"{port_result.stderr}"
            )
        port_line = port_result.stdout.strip().splitlines()[0]
        port = port_line.rsplit(":", 1)[-1]
        base_url = f"postgresql://postgres:postgres@127.0.0.1:{port}/a500test"

        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(base_url, connect_timeout=2):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    logs = subprocess.run(
                        [podman, "logs", name],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    pytest.fail(
                        "Temporary PostgreSQL did not become ready:\n"
                        f"{logs.stdout}\n{logs.stderr}"
                    )
                time.sleep(1)

        os.environ["TEST_DATABASE_URL"] = base_url
        yield base_url
    finally:
        os.environ.pop("TEST_DATABASE_URL", None)
        subprocess.run(
            [podman, "stop", name],
            capture_output=True,
            text=True,
            check=False,
        )


@contextmanager
def _isolated_postgres_url(base_url: str) -> Iterator[str]:
    """Use a private schema on the configured PostgreSQL test server."""
    parts = urlsplit(base_url)
    if parts.scheme not in ("postgres", "postgresql"):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")
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
            db.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )


@pytest.fixture
def postgres_url(test_database_url: str) -> Iterator[str]:
    with _isolated_postgres_url(test_database_url) as url:
        yield url


@pytest.fixture
def postgres_url_other(test_database_url: str) -> Iterator[str]:
    with _isolated_postgres_url(test_database_url) as url:
        yield url
