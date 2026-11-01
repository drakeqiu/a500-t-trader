import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from a500_t_trader.daily_indicators import calculate_indicators
from a500_t_trader.market_data import DailyBar

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _bars(closes: list[str]) -> tuple[DailyBar, ...]:
    return tuple(
        DailyBar(
            "510300",
            datetime(2026, 1, 1, tzinfo=SHANGHAI) + timedelta(days=index),
            Decimal(close),
            Decimal(close),
            Decimal(close),
            Decimal(close),
            100,
        )
        for index, close in enumerate(closes)
    )


def test_indicators_use_completed_observations_and_wilder_rsi():
    closes = [str(value) for value in range(1, 22)]
    rows = calculate_indicators(_bars(closes))
    assert rows[18].sma_5 == Decimal("17")
    assert rows[18].sma_20 is None
    assert rows[19].sma_20 == Decimal("10.5")
    assert rows[14].rsi_14 == Decimal("100")


def test_indicators_reject_duplicate_or_out_of_order_daily_observations():
    bars = _bars(["1", "2", "3"])
    with pytest.raises(ValueError, match="ordered"):
        calculate_indicators((bars[1], bars[0], bars[2]))


def test_daily_indicators_cli_returns_calculated_values(tmp_path, capsys):
    from a500_t_trader.__main__ import main

    class DailyData:
        def get_daily_history(self, symbol, start_date, end_date):
            assert symbol == "510300"
            assert (start_date, end_date) == (date(2026, 1, 1), date(2026, 1, 21))
            return _bars([str(value) for value in range(1, 22)])

    database = tmp_path / "history.sqlite3"
    args = [
        "daily-indicators",
        "--symbol",
        "510300",
        "--start-date",
        "2026-01-01",
        "--end-date",
        "2026-01-21",
        "--database",
        str(database),
        "--confirm-history-persistence-permitted",
    ]
    assert main(args, market_data=DailyData()) == 0
    output = json.loads(capsys.readouterr().out)
    assert output[14]["rsi_14"] == "100"
    assert output[18]["sma_5"] == "17"
    assert output[19]["sma_20"] == "10.5"

    class UnexpectedData:
        def get_daily_history(self, *args):
            raise AssertionError("cached rows should be reused")

    assert main(args, market_data=UnexpectedData()) == 0
    assert json.loads(capsys.readouterr().out) == output
