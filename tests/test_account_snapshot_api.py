from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from fastapi.testclient import TestClient

from a500_t_trader.account_snapshot_api import AccountSnapshotStore, create_app


class TestAuthenticator:
    def __call__(self, token: str) -> str:
        if token not in {"alice-token", "bob-token"}:
            raise ValueError("invalid bearer token")
        return token.removesuffix("-token")


SNAPSHOT: Mapping[str, object] = {
    "as_of": "2026-11-04T09:30:00+08:00",
    "holdings": {"510300": 100},
    "protected_floor": {"510300": 20},
    "sellable": {"510300": 80},
    "cost_basis": {"510300": "3.8500"},
    "available_cash": "125.50",
}


def test_account_snapshot_requires_bearer_credentials(tmp_path: Path) -> None:
    with TestClient(
        create_app(tmp_path / "accounts.sqlite3", TestAuthenticator())
    ) as client:
        assert client.get("/v1/account-snapshot").status_code == 401
        assert (
            client.get(
                "/v1/account-snapshot", headers={"Authorization": "Bearer invalid"}
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/v1/account-snapshot",
                headers={"Authorization": "bearer alice-token"},
            ).status_code
            == 404
        )


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
        assert (
            client.get(
                "/v1/account-snapshot", headers={"Authorization": "Bearer alice-token"}
            ).status_code
            == 404
        )


def test_put_requires_create_precondition_and_returns_etag(tmp_path: Path) -> None:
    with TestClient(
        create_app(tmp_path / "accounts.sqlite3", TestAuthenticator())
    ) as client:
        headers = {"Authorization": "Bearer alice-token"}
        assert (
            client.put(
                "/v1/account-snapshot", json=SNAPSHOT, headers=headers
            ).status_code
            == 428
        )
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


def test_put_requires_matching_etag_and_keeps_successful_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "accounts.sqlite3"
    store = AccountSnapshotStore(database)
    with TestClient(create_app(database, TestAuthenticator())) as client:
        headers = {"Authorization": "Bearer alice-token"}
        first = client.put(
            "/v1/account-snapshot",
            json=SNAPSHOT,
            headers={**headers, "If-None-Match": "*"},
        )
        assert first.status_code == 201
        assert (
            client.put(
                "/v1/account-snapshot", json=SNAPSHOT, headers=headers
            ).status_code
            == 428
        )
        changed = {
            **SNAPSHOT,
            "holdings": {"510300": 200},
            "protected_floor": {"510300": 20},
            "sellable": {"510300": 180},
        }
        replacement = client.put(
            "/v1/account-snapshot",
            json=changed,
            headers={**headers, "If-Match": first.headers["ETag"]},
        )
        assert replacement.status_code == 200
        assert replacement.headers["ETag"] != first.headers["ETag"]
        assert client.get("/v1/account-snapshot", headers=headers).json()[
            "holdings"
        ] == {"510300": 200}
        assert (
            client.put(
                "/v1/account-snapshot",
                json=SNAPSHOT,
                headers={**headers, "If-Match": first.headers["ETag"]},
            ).status_code
            == 412
        )
        history = store.history_for_subject("alice")
        assert [record["snapshot"] for record in history] == [SNAPSHOT, changed]
        assert all(
            record["classification"] == "user-entered; not broker-verified"
            for record in history
        )
        assert all(record["updated_at"] for record in history)


def test_put_empty_holdings_are_valid_and_missing_match_is_precondition_failure(
    tmp_path: Path,
) -> None:
    with TestClient(
        create_app(tmp_path / "accounts.sqlite3", TestAuthenticator())
    ) as client:
        headers = {"Authorization": "Bearer alice-token"}
        assert (
            client.put(
                "/v1/account-snapshot",
                json=SNAPSHOT,
                headers={**headers, "If-Match": '"stale"'},
            ).status_code
            == 412
        )
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


def test_put_rejects_invalid_json_with_problem_400(tmp_path: Path) -> None:
    with TestClient(
        create_app(tmp_path / "accounts.sqlite3", TestAuthenticator())
    ) as client:
        for content in ('{"broken":', "NaN"):
            response = client.put(
                "/v1/account-snapshot",
                content=content,
                headers={
                    "Authorization": "Bearer alice-token",
                    "If-None-Match": "*",
                    "Content-Type": "application/json",
                },
            )
            assert response.status_code == 400
            assert response.headers["content-type"].startswith(
                "application/problem+json"
            )
            assert response.json()["status"] == 400
            assert response.json()["errors"][0]["code"] == "invalid_json"


def test_put_rejects_invalid_contract_as_problem_422(tmp_path: Path) -> None:
    database = tmp_path / "accounts.sqlite3"
    store = AccountSnapshotStore(database)
    with TestClient(create_app(database, TestAuthenticator())) as client:
        headers = {
            "Authorization": "Bearer alice-token",
            "If-None-Match": "*",
        }
        invalid_snapshots = [
            (
                {**SNAPSHOT, "as_of": "2026-02-30T23:59:59Z"},
                "as_of",
                "invalid_timestamp",
            ),
            ({**SNAPSHOT, "extra": 1}, "extra", "unknown_field"),
            (
                {
                    key: item
                    for key, item in SNAPSHOT.items()
                    if key != "available_cash"
                },
                "available_cash",
                "missing_field",
            ),
            ({**SNAPSHOT, "available_cash": None}, "available_cash", "invalid_type"),
            (
                {**SNAPSHOT, "as_of": "2026-11-04 09:30:00+08:00"},
                "as_of",
                "invalid_timestamp",
            ),
            (
                {**SNAPSHOT, "as_of": "2026-11-04T09:30:00"},
                "as_of",
                "invalid_timestamp",
            ),
            ({**SNAPSHOT, "holdings": None}, "holdings", "invalid_type"),
            (
                {**SNAPSHOT, "holdings": {"510300": True}},
                "holdings.510300",
                "invalid_quantity",
            ),
            (
                {**SNAPSHOT, "holdings": {"510300": 1.5}},
                "holdings.510300",
                "invalid_quantity",
            ),
            (
                {**SNAPSHOT, "holdings": {"510300": -1}},
                "holdings.510300",
                "invalid_quantity",
            ),
            (
                {**SNAPSHOT, "protected_floor": {"510300": 101}},
                "protected_floor.510300",
                "quantity_inconsistent",
            ),
            (
                {**SNAPSHOT, "sellable": {"510300": 81}},
                "sellable.510300",
                "quantity_inconsistent",
            ),
            ({**SNAPSHOT, "sellable": {}}, "sellable.510300", "quantity_key_mismatch"),
            ({**SNAPSHOT, "cost_basis": []}, "cost_basis", "invalid_type"),
            (
                {**SNAPSHOT, "cost_basis": {"510300": None}},
                "cost_basis.510300",
                "invalid_type",
            ),
            (
                {**SNAPSHOT, "cost_basis": {"510300": "NaN"}},
                "cost_basis.510300",
                "invalid_decimal",
            ),
            ({**SNAPSHOT, "available_cash": "-1"}, "available_cash", "invalid_decimal"),
            ({**SNAPSHOT, "available_cash": " 1"}, "available_cash", "invalid_decimal"),
            (
                {**SNAPSHOT, "cost_basis": {"510301": "3.85"}},
                "cost_basis.510301",
                "invalid_cost_basis_key",
            ),
            (
                {**SNAPSHOT, "holdings": {"510300": 100, "SH.510300": 2}},
                "holdings.SH.510300",
                "symbol_collision",
            ),
            (
                {**SNAPSHOT, "protected_floor": {"510300": 20, "510301": 0}},
                "protected_floor.510301",
                "quantity_key_mismatch",
            ),
        ]
        for snapshot, path, code in invalid_snapshots:
            response = client.put(
                "/v1/account-snapshot", json=snapshot, headers=headers
            )
            assert response.status_code == 422
            assert response.headers["content-type"].startswith(
                "application/problem+json"
            )
            assert response.json()["errors"][0]["path"] == path
            assert response.json()["errors"][0]["code"] == code
        assert (
            client.get(
                "/v1/account-snapshot", headers={"Authorization": "Bearer alice-token"}
            ).status_code
            == 404
        )
        assert store.history_for_subject("alice") == []
        created = client.put(
            "/v1/account-snapshot",
            json=SNAPSHOT,
            headers={**headers, "If-None-Match": "*"},
        )
        assert created.status_code == 201
        invalid_update = client.put(
            "/v1/account-snapshot",
            json={**SNAPSHOT, "available_cash": "NaN"},
            headers={**headers, "If-Match": created.headers["ETag"]},
        )
        assert invalid_update.status_code == 422
        assert (
            client.get(
                "/v1/account-snapshot", headers={"Authorization": "Bearer alice-token"}
            ).json()["holdings"]
            == SNAPSHOT["holdings"]
        )
        assert len(store.history_for_subject("alice")) == 1


def test_put_accepts_backdated_rfc3339_and_normalizes_symbols(tmp_path: Path) -> None:
    snapshot = {
        **SNAPSHOT,
        "as_of": "2001-01-01T00:00:00Z",
        "holdings": {"SH.510300": 100},
        "protected_floor": {"510300": 20},
        "sellable": {"510300": 80},
        "cost_basis": {"510300": "3.85"},
    }
    with TestClient(
        create_app(tmp_path / "accounts.sqlite3", TestAuthenticator())
    ) as client:
        response = client.put(
            "/v1/account-snapshot",
            json=snapshot,
            headers={"Authorization": "Bearer alice-token", "If-None-Match": "*"},
        )
    assert response.status_code == 201
    assert response.json()["as_of"] == snapshot["as_of"]
    assert response.json()["holdings"] == {"510300": 100}
    assert response.json()["classification"] == "user-entered; not broker-verified"


def test_put_accepts_rfc3339_leap_second_through_api(tmp_path: Path) -> None:
    snapshot = {**SNAPSHOT, "as_of": "2016-12-31T23:59:60Z"}
    with TestClient(
        create_app(tmp_path / "accounts.sqlite3", TestAuthenticator())
    ) as client:
        response = client.put(
            "/v1/account-snapshot",
            json=snapshot,
            headers={"Authorization": "Bearer alice-token", "If-None-Match": "*"},
        )
    assert response.status_code == 201
    assert response.json()["as_of"] == snapshot["as_of"]
