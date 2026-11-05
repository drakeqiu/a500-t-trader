from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from a500_t_trader.account_snapshot_api import AccountSnapshotStore, create_app


class TestAuthenticator:
    def __call__(self, token: str) -> str:
        if token not in {"alice-token", "bob-token"}:
            raise ValueError("invalid bearer token")
        return token.removesuffix("-token")


SNAPSHOT = {
    "as_of": "2026-11-04T09:30:00+08:00",
    "holdings": {"510300": 100},
    "protected_floor": {"510300": 20},
    "sellable": {"510300": 80},
    "cost_basis": {"510300": "3.8500"},
    "available_cash": "125.50",
}


def test_account_snapshot_requires_bearer_credentials(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "accounts.sqlite3", TestAuthenticator())) as client:
        assert client.get("/v1/account-snapshot").status_code == 401
        assert client.get(
            "/v1/account-snapshot", headers={"Authorization": "Bearer invalid"}
        ).status_code == 401
        assert client.get(
            "/v1/account-snapshot",
            headers={"Authorization": "bearer alice-token"},
        ).status_code == 404


def test_account_snapshot_is_private_and_reports_user_entered_status(
    tmp_path: Path,
) -> None:
    database = tmp_path / "accounts.sqlite3"
    AccountSnapshotStore(database).save_snapshot("alice", SNAPSHOT)
    with TestClient(create_app(database, TestAuthenticator())) as client:
        alice = {"Authorization": "Bearer alice-token"}
        bob = {"Authorization": "Bearer bob-token"}
        response = client.get("/v1/account-snapshot", headers=alice)
        assert response.status_code == 200
        assert response.json()["as_of"] == SNAPSHOT["as_of"]
        assert response.json()["holdings"] == {"510300": 100}
        assert response.json()["updated_at"]
        assert response.json()["classification"] == "user-entered; not broker-verified"
        assert response.headers["etag"]
        assert client.get("/v1/account-snapshot", headers=bob).status_code == 404


def test_account_snapshot_returns_404_when_no_snapshot_exists(tmp_path: Path) -> None:
    with TestClient(
        create_app(tmp_path / "accounts.sqlite3", TestAuthenticator())
    ) as client:
        response = client.get(
            "/v1/account-snapshot",
            headers={"Authorization": "Bearer alice-token"},
        )
        assert response.status_code == 404


def test_account_snapshot_does_not_accept_client_owner_identifiers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "accounts.sqlite3"
    AccountSnapshotStore(database).save_snapshot("alice", SNAPSHOT)
    with TestClient(create_app(database, TestAuthenticator())) as client:
        response = client.get(
            "/v1/account-snapshot?user_id=alice&account_id=1",
            headers={"Authorization": "Bearer bob-token"},
        )
        assert response.status_code == 404


def test_account_snapshot_storage_is_isolated_by_database_file(tmp_path: Path) -> None:
    AccountSnapshotStore(tmp_path / "first.sqlite3").save_snapshot("alice", SNAPSHOT)
    with TestClient(
        create_app(tmp_path / "second.sqlite3", TestAuthenticator())
    ) as client:
        assert client.get(
            "/v1/account-snapshot", headers={"Authorization": "Bearer alice-token"}
        ).status_code == 404

def test_put_requires_create_precondition_and_returns_etag(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "accounts.sqlite3", TestAuthenticator())) as client:
        headers = {"Authorization": "Bearer alice-token"}
        assert client.put("/v1/account-snapshot", json=SNAPSHOT, headers=headers).status_code == 428
        created = client.put(
            "/v1/account-snapshot",
            json=SNAPSHOT,
            headers={**headers, "If-None-Match": "*"},
        )
        assert created.status_code == 201
        assert created.json() == {
            **SNAPSHOT,
            "updated_at": created.json()["updated_at"],
            "classification": "user-entered; not broker-verified",
        }
        assert created.json()["updated_at"]
        assert created.headers["ETag"]


def test_put_requires_matching_etag_and_keeps_successful_history(tmp_path: Path) -> None:
    database = tmp_path / "accounts.sqlite3"
    with TestClient(create_app(database, TestAuthenticator())) as client:
        headers = {"Authorization": "Bearer alice-token"}
        first = client.put(
            "/v1/account-snapshot",
            json=SNAPSHOT,
            headers={**headers, "If-None-Match": "*"},
        )
        assert first.status_code == 201
        assert client.put(
            "/v1/account-snapshot", json=SNAPSHOT, headers=headers
        ).status_code == 428
        changed = {**SNAPSHOT, "holdings": {"510300": 200},
                   "protected_floor": {"510300": 20}, "sellable": {"510300": 180}}
        replacement = client.put(
            "/v1/account-snapshot",
            json=changed,
            headers={**headers, "If-Match": first.headers["ETag"]},
        )
        assert replacement.status_code == 200
        assert replacement.headers["ETag"] != first.headers["ETag"]
        assert client.get("/v1/account-snapshot", headers=headers).json()["holdings"] == {
            "510300": 200
        }
        assert client.put(
            "/v1/account-snapshot",
            json=SNAPSHOT,
            headers={**headers, "If-Match": first.headers["ETag"]},
        ).status_code == 412
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT count(*) FROM account_snapshot_history").fetchone()[0] == 2
            snapshot_json, as_of, updated_at, classification = db.execute(
                "SELECT snapshot_json,as_of,updated_at,classification "
                "FROM account_snapshot_history ORDER BY id"
            ).fetchall()[0]
            assert json.loads(snapshot_json) == SNAPSHOT
            assert as_of == SNAPSHOT["as_of"]
            assert updated_at
            assert classification == "user-entered; not broker-verified"


def test_put_empty_holdings_are_valid_and_missing_match_is_precondition_failure(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(tmp_path / "accounts.sqlite3", TestAuthenticator())) as client:
        headers = {"Authorization": "Bearer alice-token"}
        assert client.put(
            "/v1/account-snapshot",
            json=SNAPSHOT,
            headers={**headers, "If-Match": '"stale"'},
        ).status_code == 412
        empty = {
            "as_of": SNAPSHOT["as_of"],
            "holdings": {},
            "protected_floor": {},
            "sellable": {},
            "cost_basis": {},
            "available_cash": "0",
        }
        created = client.put(
            "/v1/account-snapshot",
            json=empty,
            headers={**headers, "If-None-Match": "*"},
        )
        assert created.status_code == 201
        assert created.json()["holdings"] == {}
        assert client.delete("/v1/account-snapshot", headers=headers).status_code == 405
