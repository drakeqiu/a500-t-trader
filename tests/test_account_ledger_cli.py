import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from a500_t_trader.cli.main import main

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_manual_fill_cli_persists_user_record_separately(
    tmp_path: Path, capsys: Any
) -> None:
    database = tmp_path / "account.sqlite3"
    result = main(
        [
            "manual-fill",
            "--database",
            str(database),
            "--timestamp",
            "2026-10-30T10:01:00+08:00",
            "--symbol",
            "510300.SH",
            "--side",
            "buy",
            "--quantity",
            "100",
            "--price",
            "3.85",
            "--notes",
            "entered by user",
        ]
    )
    assert result == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["classification"] == "user-entered manual fill; not broker-verified"

    assert main(["account-ledger", "--database", str(database)]) == 0
    ledger = json.loads(capsys.readouterr().out)
    assert ledger["manual_fills"][0]["symbol"] == "510300"
    assert ledger["manual_fills"][0]["quantity"] == 100
    assert ledger["manual_fills"][0]["price"] == "3.85"


def test_manual_fill_requires_timezone_and_positive_order_values(
    tmp_path: Path, capsys: Any
) -> None:
    result = main(
        [
            "manual-fill",
            "--database",
            str(tmp_path / "invalid.sqlite3"),
            "--timestamp",
            "2026-10-30T10:01:00",
            "--symbol",
            "510300",
            "--side",
            "sell",
            "--quantity",
            "0",
            "--price",
            "-1",
        ]
    )
    assert result == 2
    assert "timezone" in capsys.readouterr().err


def test_galaxy_import_preview_confirmation_and_postmarket_provenance(
    tmp_path: Path, capsys: Any
) -> None:
    database = tmp_path / "account.sqlite3"
    source = tmp_path / "galaxy.csv"
    source.write_bytes(
        (
            "成交日期,成交时间,证券代码,证券名称,操作,成交数量,成交均价,成交金额,"
            "合同编号,成交编号,手续费,印花税,其他杂费,发生金额,资金余额,交易市场,"
            "股东帐户,交收日期,证券中文全称,\n"
            "20261030,10:01:02,563360,A500基金,买入,8000,1.264,10112.000,"
            "0062015087,32057164,0.510,0.000,0.000,-10112.510,532232.790,"
            "沪Ａ,A807499773,20261030,A500ETF华泰柏瑞,\n"
        ).encode("gb18030")
    )

    assert (
        main(["import-fills", "--file", str(source), "--database", str(database)])
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["valid"] == 1
    assert preview["new"] == 1
    assert preview["duplicates"] == 0
    assert preview["errors"] == 0
    assert not database.exists()
    assert main(["account-ledger", "--database", str(database)]) == 0
    empty_ledger = json.loads(capsys.readouterr().out)
    assert empty_ledger["imported_fills"] == []
    assert empty_ledger["snapshots"] == []

    assert main(
        [
            "import-fills", "--file", str(source), "--database", str(database),
            "--confirm",
        ]
    ) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["inserted_ids"] == [1]
    assert main(["account-ledger", "--database", str(database)]) == 0
    ledger = json.loads(capsys.readouterr().out)
    fill = ledger["imported_fills"][0]
    assert ledger["manual_fills"] == []
    assert fill["timestamp"] == "2026-10-30T10:01:02+08:00"
    assert fill["symbol"] == "563360"
    assert fill["trade_amount"] == "10112.000"
    assert fill["occurrence_amount"] == "-10112.510"
    assert fill["classification"] == (
        "user-provided Galaxy Securities file; not system-verified"
    )

    plan = tmp_path / "plan.json"
    plan.write_text(
        '{"session_date":"2026-10-30","plan":{"suggestions":[]},'
        '"account_snapshot":{"available_cash":"5000"}}',
        encoding="utf-8",
    )
    assert main(
        [
            "postmarket", "--date", "2026-10-30", "--plan", str(plan),
            "--database", str(database),
        ]
    ) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["manual_fills"]["records"] == []
    reviewed = review["imported_fills"]["records"][0]
    assert reviewed["trade_number"] == "32057164"
    assert "not system-verified" in review["imported_fills"]["classification"]
    assert review["account_snapshot"]["available_cash"] == "5000"

    assert main(
        [
            "import-fills", "--file", str(source), "--database", str(database),
            "--confirm",
        ]
    ) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["new"] == 0
    assert repeated["duplicates"] == 1

    source.write_bytes(source.read_bytes().replace(b"1.264", b"1.265"))
    assert main(
        [
            "import-fills", "--file", str(source), "--database", str(database),
            "--confirm",
        ]
    ) == 2
    conflict = json.loads(capsys.readouterr().out)
    assert conflict["new"] == 0
    assert conflict["duplicates"] == 0
    assert conflict["errors"] == 1


def test_galaxy_import_rejects_whole_batch_without_echoing_sensitive_fields(
    tmp_path: Path, capsys: Any
) -> None:
    database = tmp_path / "account.sqlite3"
    source = tmp_path / "invalid.csv"
    header = (
        "成交日期,成交时间,证券代码,证券名称,操作,成交数量,成交均价,成交金额,"
        "合同编号,成交编号,手续费,印花税,其他杂费,发生金额,资金余额,交易市场,"
        "股东帐户,交收日期,证券中文全称,\n"
    )
    rows = (
        "20261030,10:01:02,563360,A500基金,买入,8000,1.264,10112.000,"
        "0062015087,32057164,0.510,0.000,0.000,-10112.510,SECRET_BALANCE,"
        "沪Ａ,SECRET_ACCOUNT,20261030,A500ETF华泰柏瑞,\n"
        "20261030,10:02:03,510300,ETF,买入,0,1.264,10112.000,"
        "0062015088,32057165,0.510,0.000,0.000,-10112.510,SECRET_BALANCE,"
        "沪Ａ,SECRET_ACCOUNT,20261030,ETF,\n"
    )
    source.write_bytes((header + rows).encode("gb18030"))

    assert main(
        [
            "import-fills", "--file", str(source), "--database", str(database),
            "--confirm",
        ]
    ) == 2
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["valid"] == 1
    assert report["new"] == 0
    assert report["errors"] == 1
    assert report["error_details"][0]["row"] == 3
    assert report["error_details"][0]["field"] == "成交数量"
    assert "SECRET_BALANCE" not in output.out + output.err
    assert "SECRET_ACCOUNT" not in output.out + output.err
    assert not database.exists()
