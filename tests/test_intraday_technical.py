from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from a500_t_trader.market_data import MinuteBar
from a500_t_trader.strategy.intraday_technical import analyze_intraday_technical

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _bars(symbol: str = "510300", count: int = 180) -> tuple[MinuteBar, ...]:
    start = datetime(2026, 10, 30, 9, 30, tzinfo=SHANGHAI)
    rows = []
    for index in range(count - 1):
        timestamp = start + timedelta(minutes=5 * index)
        close = Decimal("4.000") + Decimal(index) / Decimal("10000")
        rows.append(
            MinuteBar(
                symbol=symbol,
                timestamp=timestamp,
                interval_minutes=5,
                open=close,
                high=close + Decimal("0.010"),
                low=close - Decimal("0.010"),
                close=close,
                volume=10000,
                amount=close * 10000,
                is_completed=True,
            )
        )
    close = Decimal("4.000") + Decimal(count - 1) / Decimal("10000")
    rows.append(
        MinuteBar(
            symbol=symbol,
            timestamp=datetime(2026, 11, 2, 9, 45, tzinfo=SHANGHAI),
            interval_minutes=5,
            open=close,
            high=close + Decimal("0.010"),
            low=close - Decimal("0.010"),
            close=close,
            volume=10000,
            amount=close * 10000,
            is_completed=True,
        )
    )
    return tuple(rows)


def test_analysis_reports_trend_but_fails_closed_when_exact_scoring_rules_absent() -> None:
    analysis = analyze_intraday_technical({"510300": _bars()})["510300"]

    assert analysis.trend in {"BULL", "BEAR", "RANGE", "STRONG_BULL", "STRONG_BEAR"}
    assert analysis.indicators["ema_20"] is not None
    assert analysis.indicators["bollinger_upper"] is not None
    assert analysis.indicators["kdj_k"] is not None
    assert analysis.indicators["atr_14"] is not None
    assert analysis.indicators["vwap_0945"] is not None
    assert analysis.scores == {"buy": None, "sell": None}
    assert analysis.candidate == "WAIT"
    assert analysis.risk_status == "BLOCKED"
    assert any("exact V1.0 scoring rules" in reason for reason in analysis.reasons)


def test_incomplete_bars_do_not_satisfy_180_bar_gate() -> None:
    bars = _bars(count=180)
    with_incomplete = bars[:-1] + (replace(bars[-1], is_completed=False),)

    analysis = analyze_intraday_technical({"510300": with_incomplete})["510300"]

    assert analysis.candidate == "WAIT"
    assert "fewer than 180 completed valid 5-minute bars" in analysis.reasons


def test_invalid_history_isolated_per_symbol() -> None:
    good = _bars()
    short = _bars("510500", count=179)

    results = analyze_intraday_technical({"510300": good, "510500": short})

    assert results["510300"].indicators["ema_20"] is not None
    assert results["510500"].candidate == "WAIT"
    assert "fewer than 180 completed valid 5-minute bars" in results["510500"].reasons


def test_missing_0945_anchor_is_not_replaced_with_later_vwap() -> None:
    bars = tuple(bar for bar in _bars() if bar.timestamp.strftime("%H:%M") != "09:45")

    analysis = analyze_intraday_technical({"510300": bars})["510300"]

    assert analysis.indicators["vwap_0945"] is None
    assert "missing 09:45 VWAP anchor" in analysis.reasons
    assert analysis.candidate == "WAIT"
