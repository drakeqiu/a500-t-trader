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


GALAXY_HEADER = (
    "成交日期,成交时间,证券代码,证券名称,操作,成交数量,成交均价,成交金额,"
    "合同编号,成交编号,手续费,印花税,其他杂费,发生金额,资金余额,交易市场,"
    "股东帐户,交收日期,证券中文全称,\n"
)


def _galaxy_csv(*rows: str) -> bytes:
    return (
        GALAXY_HEADER
        + "".join(
            "20261030,10:01:02,563360,A500基金,买入,8000,1.264,10112,"
            f"contract-{number},{number},0.510,0.000,0.000,-10112.510,"
            "532232.790,沪Ａ,PRIVATE-ACCOUNT,20261030,A500ETF华泰柏瑞,\n"
            for number in rows
        )
    ).encode("gb18030")


def test_trade_import_preview_requires_authentication(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "accounts.sqlite3", TestAuthenticator())) as client:
        response = client.post(
            "/v1/trade-imports/previews",
            files={"file": ("fills.csv", _galaxy_csv("1001"), "text/csv")},
        )
    assert response.status_code == 401


def test_trade_import_preview_is_owner_scoped_and_does_not_write_fills(
    tmp_path: Path,
) -> None:
    database = tmp_path / "accounts.sqlite3"
    with TestClient(create_app(database, TestAuthenticator())) as client:
        alice = {"Authorization": "Bearer alice-token"}
        bob = {"Authorization": "Bearer bob-token"}
        alice_result = client.post(
            "/v1/trade-imports/previews",
            headers=alice,
            files={"file": ("fills.csv", _galaxy_csv("1001"), "text/csv")},
        )
        bob_result = client.post(
            "/v1/trade-imports/previews",
            headers=bob,
            files={"file": ("fills.csv", _galaxy_csv("1001"), "text/csv")},
        )
        assert alice_result.status_code == bob_result.status_code == 200
        for response in (alice_result, bob_result):
            assert response.json()["valid"] == 1
            assert response.json()["new"] == 1
            assert response.json()["duplicates"] == 0
            assert response.json()["errors"] == 0
            assert response.json()["batch_id"]
        assert alice_result.json()["batch_id"] != bob_result.json()["batch_id"]

    from a500_t_trader.account_ledger import AccountLedger

    assert AccountLedger(database).imported_fills() == []
    import sqlite3

    with sqlite3.connect(database) as db:
        batch = db.execute(
            "SELECT owner,records_json FROM trade_import_batches"
        ).fetchall()
    assert {owner for owner, _ in batch} == {"alice", "bob"}
    assert all("PRIVATE-ACCOUNT" not in records for _, records in batch)


def test_trade_import_preview_sanitizes_validation_errors(tmp_path: Path) -> None:
    content = _galaxy_csv("1001").replace(b",8000,1.264,", b",0,1.264,")
    with TestClient(create_app(tmp_path / "accounts.sqlite3", TestAuthenticator())) as client:
        response = client.post(
            "/v1/trade-imports/previews",
            headers={"Authorization": "Bearer alice-token"},
            files={"file": ("fills.csv", content, "text/csv")},
        )
    assert response.status_code == 200
    assert response.json()["valid"] == 0
    assert response.json()["errors"] == 1
    assert "PRIVATE-ACCOUNT" not in response.text


def test_trade_import_preview_rejects_large_upload_and_unsupported_header(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(tmp_path / "accounts.sqlite3", TestAuthenticator())) as client:
        headers = {"Authorization": "Bearer alice-token"}
        oversized = client.post(
            "/v1/trade-imports/previews",
            headers=headers,
            files={"file": ("fills.csv", b"x" * (5 * 1024 * 1024 + 1), "text/csv")},
        )
        unsupported = client.post(
            "/v1/trade-imports/previews",
            headers=headers,
            files={"file": ("fills.csv", b"account,password\nsecret,hidden\n", "text/csv")},
        )
    assert oversized.status_code == 413
    assert unsupported.status_code == 422
    assert "secret" not in unsupported.text
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
