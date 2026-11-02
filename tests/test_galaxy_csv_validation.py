import json
from pathlib import Path
from typing import Any

from a500_t_trader.cli.main import main

HEADER = (
    "成交日期,成交时间,证券代码,证券名称,操作,成交数量,成交均价,成交金额,"
    "合同编号,成交编号,手续费,印花税,其他杂费,发生金额,资金余额,交易市场,"
    "股东帐户,交收日期,证券中文全称,\n"
)


def test_import_preserves_exact_decimal_values_and_ignores_blank_rows(
    tmp_path: Path, capsys: Any
) -> None:
    source = tmp_path / "precise.csv"
    database = tmp_path / "account.sqlite3"
    row = (
        "20261030,09:01:02,000001,示例,卖出,1,0.123456789012345678901,"
        "12345678901234567890.123456789,contract,trade,0.000000000000000001,"
        "0.000000000000000002,0.000000000000000003,-12345678901234567890.987654321,"
        "SENSITIVE_BALANCE,沪Ａ,SENSITIVE_ACCOUNT,20261030,秘密证券全称,\n"
    )
    source.write_bytes((HEADER + "," * 20 + "\n\n" + row).encode("gb18030"))

    assert main([
        "import-fills", "--file", str(source), "--database", str(database), "--confirm"
    ]) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["valid"] == 1
    assert report["errors"] == 0
    assert all(secret not in output.out + output.err for secret in (
        "SENSITIVE_BALANCE", "SENSITIVE_ACCOUNT", "秘密证券全称"
    ))

    assert main(["account-ledger", "--database", str(database)]) == 0
    fill = json.loads(capsys.readouterr().out)["imported_fills"][0]
    assert fill["symbol"] == "000001"
    assert fill["price"] == "0.123456789012345678901"
    assert fill["trade_amount"] == "12345678901234567890.123456789"
    assert fill["fees"] == "1E-18"
    assert fill["stamp_duty"] == "2E-18"
    assert fill["other_charges"] == "3E-18"
    assert fill["occurrence_amount"] == "-12345678901234567890.987654321"
    assert "资金余额" not in fill
    assert "SENSITIVE" not in json.dumps(fill, ensure_ascii=False)


def test_import_rejects_unsupported_structure_and_strict_encoding_without_echo(
    tmp_path: Path, capsys: Any
) -> None:
    database = tmp_path / "account.sqlite3"
    source = tmp_path / "unsupported.csv"
    source.write_bytes(
        (HEADER.replace("成交时间", "时间", 1) + "private row\n").encode("gb18030")
    )
    assert (
        main(["import-fills", "--file", str(source), "--database", str(database)])
        == 2
    )
    output = capsys.readouterr()
    assert "unsupported Galaxy CSV header" in output.out
    assert "private row" not in output.out + output.err
    assert not database.exists()

    source.write_bytes(HEADER.encode("gb18030") + b"\xff\n")
    assert (
        main(["import-fills", "--file", str(source), "--database", str(database)])
        == 2
    )
    output = capsys.readouterr()
    assert "cannot read strict GB18030 CSV" in output.out
    assert "\\xff" not in output.out + output.err
    assert not database.exists()


def test_import_rejects_noncanonical_timestamp_and_malformed_row_privately(
    tmp_path: Path, capsys: Any
) -> None:
    database = tmp_path / "account.sqlite3"
    source = tmp_path / "invalid.csv"
    row = (
        "2026103,09:01:02,000001,名称,买入,1,1.25,1.25,contract,trade,"
        "0,0,0,-1,SENSITIVE_BALANCE,沪Ａ,SENSITIVE_ACCOUNT,20261030,名称,\n"
    )
    source.write_bytes((HEADER + row).encode("gb18030"))
    assert main([
        "import-fills", "--file", str(source), "--database", str(database), "--confirm"
    ]) == 2
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["error_details"][0]["row"] == 2
    assert report["error_details"][0]["field"] == "成交日期/成交时间"
    assert "SENSITIVE_BALANCE" not in output.out + output.err
    assert "SENSITIVE_ACCOUNT" not in output.out + output.err
    assert not database.exists()

def test_import_rejects_nonfinite_values_and_invalid_security_codes(
    tmp_path: Path, capsys: Any
) -> None:
    database = tmp_path / "account.sqlite3"
    source = tmp_path / "invalid-values.csv"
    rows = (
        "20261030,09:01:02,000001,名称,买入,1,NaN,1,contract,trade1,"
        "0,0,0,-1,SENSITIVE_BALANCE,沪Ａ,SENSITIVE_ACCOUNT,20261030,名称,\n"
        "20261030,09:01:02,12,名称,买入,1,1,1,contract,trade2,"
        "0,0,0,-1,SENSITIVE_BALANCE,沪Ａ,SENSITIVE_ACCOUNT,20261030,名称,\n"
    )
    source.write_bytes((HEADER + rows).encode("gb18030"))

    assert main([
        "import-fills", "--file", str(source), "--database", str(database), "--confirm"
    ]) == 2
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["errors"] == 2
    assert [(error["row"], error["field"]) for error in report["error_details"]] == [
        (2, "成交均价"), (3, "证券代码")
    ]
    assert "NaN" not in output.out + output.err
    assert "SENSITIVE_BALANCE" not in output.out + output.err
    assert "SENSITIVE_ACCOUNT" not in output.out + output.err
    assert not database.exists()
