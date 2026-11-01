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
