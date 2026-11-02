from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from a500_t_trader.market_data import MinuteBar
from a500_t_trader.strategy.intraday_technical import (
    TECHNICAL_PARAMETER_HASH,
    TECHNICAL_PARAMETERS,
    analyze_intraday_technical,
)
from a500_t_trader.strategy.intraday_v1_config import IntradayV1Config

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


def test_indicator_warmups_use_exact_period_boundaries() -> None:
    at_warmup = analyze_intraday_technical({"510300": _bars(count=60)})["510300"]
    before_ema60 = analyze_intraday_technical({"510300": _bars(count=59)})["510300"]

    assert at_warmup.indicators["ema_20"] is not None
    assert at_warmup.indicators["ema_60"] is not None
    assert at_warmup.indicators["bollinger_sigma"] is not None
    assert at_warmup.indicators["kdj_k"] is not None
    assert at_warmup.indicators["atr_14"] is not None
    assert before_ema60.indicators["ema_60"] is None
    assert (
        analyze_intraday_technical({"510300": _bars(count=20)})["510300"].indicators[
            "bollinger_sigma"
        ]
        is not None
    )
    assert (
        analyze_intraday_technical({"510300": _bars(count=9)})["510300"].indicators[
            "kdj_k"
        ]
        is not None
    )
    assert (
        analyze_intraday_technical({"510300": _bars(count=15)})["510300"].indicators[
            "atr_14"
        ]
        is not None
    )


def test_bollinger_score_saturates_at_source_z_boundary() -> None:
    source = _bars()
    bars = source[:-1] + (
        replace(
            source[-1],
            open=Decimal("4.000"),
            high=Decimal("4.010"),
            low=Decimal("3.980"),
            close=Decimal("3.990"),
            amount=Decimal("39900"),
        ),
    )
    analysis = analyze_intraday_technical({"510300": bars})["510300"]


    score_breakdown = analysis.score_breakdown
    assert score_breakdown is not None
    assert score_breakdown["boll_buy"] == 20.0
    assert score_breakdown["boll_sell"] == 0.0


def test_flat_completed_history_keeps_range_baseline_scores_and_wait() -> None:
    bars = tuple(
        replace(
            bar,
            open=Decimal("4.000"),
            high=Decimal("4.010"),
            low=Decimal("3.990"),
            close=Decimal("4.000"),
            amount=Decimal("40000"),
        )
        for bar in _bars()
    )
    analysis = analyze_intraday_technical({"510300": bars})["510300"]

    assert analysis.trend == "RANGE"
    assert analysis.scores == {"buy": 10.0, "sell": 10.0, "delta": 0.0}
    assert analysis.candidate == "WAIT"
    assert analysis.risk_status == "BLOCKED"
    assert any(
        "dynamic grid inputs unavailable" in reason for reason in analysis.reasons
    )


def test_scoring_emits_config_and_version_fingerprint() -> None:
    config = IntradayV1Config(
        strategy_id="intraday-etf-t-v1",
        rule_version="V1.0",
        implementation_version="commit:test",
        rule_source="tracked V1 rules",
        rule_source_sha256="6c71ca0892a76f4c479454107ace005bbdec2dd3f96d823727b44ae0e01c1689",
        parameters={},
        certification={},
        fees={},
        account={},
        securities={},
        fingerprint="a" * 64,
    )
    analysis = analyze_intraday_technical({"510300": _bars()}, config=config)["510300"]

    assert analysis.parameter_version == "intraday-etf-t-v1"
    assert analysis.parameter_hash == config.fingerprint


def test_grid_anchor_uses_session_context_and_retains_step_between_rechecks() -> None:
    bars = tuple(
        replace(
            bar,
            open=Decimal("4.000"),
            high=Decimal("4.010"),
            low=Decimal("3.990"),
            close=Decimal("4.000"),
            amount=Decimal("40000"),
        )
        for bar in _bars()
    )
    analysis = analyze_intraday_technical(
        {"510300": bars},
        grid_context={
            "510300": {
                "tick_size": Decimal("0.001"),
                "round_trip_cost_rate": Decimal("0.0001"),
                "grid_date": bars[-1].timestamp.date(),
                "current_step": Decimal("0.020"),
            }
        },
    )["510300"]

    assert analysis.indicators["grid_anchor"] == Decimal("4.000")
    assert analysis.grid_step == Decimal("0.020")
    assert analysis.parameter_hash == TECHNICAL_PARAMETER_HASH


def test_dynamic_grid_width_over_v1_limit_blocks_scoring_candidate() -> None:
    bars = tuple(
        replace(
            bar,
            open=Decimal("4.000"),
            high=Decimal("4.050"),
            low=Decimal("3.950"),
            close=Decimal("4.000"),
            amount=Decimal("40000"),
        )
        for bar in _bars()
    )

    analysis = analyze_intraday_technical(
        {"510300": bars},
        grid_context={
            "510300": {
                "tick_size": Decimal("0.001"),
                "round_trip_cost_rate": Decimal("0.0001"),
            }
        },
    )["510300"]

    assert analysis.candidate == "WAIT"
    assert analysis.grid_step is None
    assert analysis.risk_status == "BLOCKED"
    assert (
        "dynamic grid inputs unavailable or grid width exceeds 0.90%"
        in analysis.reasons
    )


def test_normal_signal_window_closes_after_protection_only_cutoff() -> None:
    source = _bars()
    bars = source + (
        replace(source[-1], timestamp=source[-1].timestamp.replace(hour=14, minute=50)),
    )
    analysis = analyze_intraday_technical(
        {"510300": bars},
        grid_context={
            "510300": {
                "tick_size": Decimal("0.001"),
                "round_trip_cost_rate": Decimal("0.0001"),
            }
        },
    )["510300"]

    assert analysis.candidate == "WAIT"
    assert analysis.risk_status == "READY"
    assert "outside V1.0 technical signal time windows" in analysis.reasons


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


def test_source_exact_paired_targets_emit_take_profit_and_buyback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import a500_t_trader.strategy.intraday_technical as technical

    monkeypatch.setattr(
        technical, "_indicators", lambda _bars: {"vwap_0945": Decimal("4.00")}
    )
    monkeypatch.setattr(technical, "_trend", lambda *_args: "RANGE")
    monkeypatch.setattr(
        technical,
        "_grid",
        lambda *_args: (Decimal("0.01"), 0.0, 0.0, None, None),
    )
    monkeypatch.setattr(technical, "_confirmations", lambda *_args: (True, True))
    monkeypatch.setattr(technical, "_window_allows", lambda *_args: (True, True))

    for buy_score, sell_score, pair_key, target, expected in (
        (50.0, 80.0, "take_profit", Decimal("4.00"), "TAKE_PROFIT"),
        (80.0, 50.0, "buyback", Decimal("4.04"), "BUYBACK"),
    ):
        monkeypatch.setattr(
            technical,
            "_score_components",
            lambda *_args, buy=buy_score, sell=sell_score: {
                "buy_total": buy,
                "sell_total": sell,
            },
        )
        analysis = analyze_intraday_technical(
            {"510300": _bars()},
            grid_context={
                "510300": {
                    "tick_size": Decimal("0.001"),
                    "round_trip_cost_rate": Decimal("0.0001"),
                }
            },
            as_of=datetime(2026, 11, 2, 10, 0, tzinfo=SHANGHAI),
            paired_state={
                "510300": {
                    pair_key: {
                        "target_price": target,
                        "quantity": 100,
                        "trade_pair_id": "pair-1",
                    }
                }
            },
        )["510300"]
        assert analysis.candidate == expected


def test_paired_target_does_not_override_ordinary_buy_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import a500_t_trader.strategy.intraday_technical as technical

    monkeypatch.setattr(
        technical, "_indicators", lambda _bars: {"vwap_0945": Decimal("4.00")}
    )
    monkeypatch.setattr(technical, "_trend", lambda *_args: "RANGE")
    monkeypatch.setattr(
        technical,
        "_score_components",
        lambda *_args: {"buy_total": 80.0, "sell_total": 50.0},
    )
    monkeypatch.setattr(
        technical,
        "_grid",
        lambda *_args: (Decimal("0.01"), 0.0, 0.0, None, None),
    )
    monkeypatch.setattr(technical, "_confirmations", lambda *_args: (True, True))
    monkeypatch.setattr(technical, "_window_allows", lambda *_args: (True, True))

    analysis = analyze_intraday_technical(
        {"510300": _bars()},
        grid_context={
            "510300": {
                "tick_size": Decimal("0.001"),
                "round_trip_cost_rate": Decimal("0.0001"),
            }
        },
        paired_state={
            "510300": {
                "take_profit": {
                    "target_price": Decimal("4.03"),
                    "quantity": 100,
                    "trade_pair_id": "pair-1",
                }
            }
        },
    )["510300"]

    assert analysis.candidate == "BUY"
    assert analysis.candidate != "TAKE_PROFIT"


def test_same_session_missing_bar_fails_closed() -> None:
    source = _bars()
    gap_index = 10
    bars = (
        source[:gap_index]
        + (
            replace(
                source[gap_index],
                timestamp=source[gap_index].timestamp + timedelta(minutes=5),
            ),
        )
        + source[gap_index + 1 :]
    )

    analysis = analyze_intraday_technical({"510300": bars})["510300"]

    assert analysis.candidate == "WAIT"
    assert analysis.risk_status == "BLOCKED"
    assert (
        "missing completed five-minute bars within a trading session"
        in analysis.reasons
    )


def test_configured_signal_threshold_changes_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import a500_t_trader.strategy.intraday_technical as technical

    monkeypatch.setattr(
        technical, "_indicators", lambda _bars: {"vwap_0945": Decimal("4.00")}
    )
    monkeypatch.setattr(technical, "_trend", lambda *_args: "RANGE")
    monkeypatch.setattr(
        technical,
        "_score_components",
        lambda *_args: {"buy_total": 70.0, "sell_total": 40.0},
    )
    monkeypatch.setattr(
        technical,
        "_grid",
        lambda *_args: (Decimal("0.01"), 0.0, 0.0, None, None),
    )
    monkeypatch.setattr(technical, "_confirmations", lambda *_args: (True, False))
    monkeypatch.setattr(technical, "_window_allows", lambda *_args: (True, True))
    config = IntradayV1Config(
        strategy_id="intraday-etf-t-v1",
        rule_version="V1.0",
        implementation_version="commit:test",
        rule_source="tracked V1 rules",
        rule_source_sha256="6c71ca0892a76f4c479454107ace005bbdec2dd3f96d823727b44ae0e01c1689",
        parameters={
            **TECHNICAL_PARAMETERS,
            "signals": {"buy_threshold": 60, "sell_threshold": 68},
        },
        certification={},
        fees={},
        account={},
        securities={},
        fingerprint="a" * 64,
    )

    analysis = analyze_intraday_technical(
        {"510300": _bars()},
        config=config,
        grid_context={
            "510300": {
                "tick_size": Decimal("0.001"),
                "round_trip_cost_rate": Decimal("0.0001"),
            }
        },
        as_of=datetime(2026, 11, 2, 10, 0, tzinfo=SHANGHAI),
    )["510300"]

    assert analysis.candidate == "BUY"
