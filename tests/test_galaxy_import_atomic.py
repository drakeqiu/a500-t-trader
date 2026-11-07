from pathlib import Path

import psycopg
import pytest

from a500_t_trader.account_ledger import AccountLedger, import_galaxy_fills

HEADER = (
    "成交日期,成交时间,证券代码,证券名称,操作,成交数量,成交均价,成交金额,"
    "合同编号,成交编号,手续费,印花税,其他杂费,发生金额,资金余额,交易市场,"
    "股东帐户,交收日期,证券中文全称,\n"
)


def _row(trade_number: str, *, price: str = "1.264", quantity: str = "8000") -> str:
    amount = str(int(quantity) * 1264 // 1000)
    return (
        f"20261030,10:01:02,563360,A500基金,买入,{quantity},{price},{amount},"
        f"contract-{trade_number},{trade_number},0.510,0.000,0.000,-10112.510,"
        "532232.790,沪Ａ,A807499773,20261030,A500ETF华泰柏瑞,\n"
    )


def _write_source(path: Path, *rows: str) -> None:
    path.write_bytes((HEADER + "".join(rows)).encode("gb18030"))


def test_invalid_row_rejects_batch_without_writes(
    tmp_path: Path, postgres_url: str
) -> None:
    database = postgres_url
    AccountLedger(database)
    source = tmp_path / "fills.csv"
    _write_source(source, _row("1001"), _row("1002", quantity="0"))

    result = import_galaxy_fills(database, source, confirm=True)

    assert result["errors"] == 1
    assert result["new"] == 0
    assert AccountLedger(database).imported_fills() == []


def test_consistent_duplicates_and_conflicts_are_atomic(
    tmp_path: Path, postgres_url: str
) -> None:
    database = postgres_url
    AccountLedger(database)
    source = tmp_path / "fills.csv"
    _write_source(source, _row("1001"))
    assert import_galaxy_fills(database, source, confirm=True)["new"] == 1

    _write_source(source, _row("1001"), _row("1001"))
    repeated = import_galaxy_fills(database, source, confirm=True)
    assert repeated["new"] == 0
    assert repeated["duplicates"] == 2
    assert len(AccountLedger(database).imported_fills()) == 1

    _write_source(source, _row("1002"), _row("1001", price="1.265"))
    database_conflict = import_galaxy_fills(database, source, confirm=True)
    assert database_conflict["errors"] == 1
    assert database_conflict["new"] == 0
    assert len(AccountLedger(database).imported_fills()) == 1

    _write_source(source, _row("1002"), _row("1002", price="1.265"))
    file_conflict = import_galaxy_fills(database, source, confirm=True)
    assert file_conflict["errors"] == 1
    assert file_conflict["new"] == 0
    assert len(AccountLedger(database).imported_fills()) == 1


def test_database_write_failure_rolls_back_every_new_fill(
    tmp_path: Path, postgres_url: str
) -> None:
    database = postgres_url
    AccountLedger(database)
    with psycopg.connect(database) as db:
        db.execute("""
            CREATE FUNCTION reject_second_fill() RETURNS trigger AS $$
            BEGIN
                IF NEW.trade_number = '1002' THEN
                    RAISE EXCEPTION 'write failed' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        db.execute("""
            CREATE TRIGGER reject_second_fill BEFORE INSERT ON imported_fills
            FOR EACH ROW EXECUTE FUNCTION reject_second_fill()
        """)
    source = tmp_path / "fills.csv"
    _write_source(source, _row("1001"), _row("1002"))

    with pytest.raises(psycopg.IntegrityError, match="write failed"):
        import_galaxy_fills(database, source, confirm=True)

    assert AccountLedger(database).imported_fills() == []
