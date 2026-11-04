from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from a500_t_trader.cli.main import main
from a500_t_trader.market_data import MinuteBar
from a500_t_trader.strategy.intraday_technical import TECHNICAL_PARAMETERS

SHANGHAI = ZoneInfo("Asia/Shanghai")


class _Subscription:
    def __init__(self, events: tuple[MinuteBar, ...]) -> None:
        self.events = events
        self.closed = False

    def __iter__(self) -> Iterator[MinuteBar]:
        return iter(self.events)

    def close(self) -> None:
        self.closed = True


class _Provider:
    def __init__(self, events: tuple[MinuteBar, ...]) -> None:
        self.events = events
        self.closed = False

    def subscribe(
        self, symbol: str, event_type: str, callback: object | None = None
    ) -> _Subscription:
        assert event_type == "bar"
        return _Subscription(
            tuple(event for event in self.events if event.symbol == symbol)
        )

    def close(self) -> None:
        self.closed = True


def _events(symbol: str = "510300") -> tuple[MinuteBar, ...]:
    start = datetime(2026, 11, 4, 10, 0, tzinfo=SHANGHAI)
    return tuple(
        MinuteBar(
            symbol,
            start + timedelta(minutes=index),
            1,
            Decimal("4.00"),
            Decimal("4.01"),
            Decimal("3.99"),
            Decimal("4.00"),
            1000,
            Decimal("4000"),
            True,
        )
        for index in range(5)
    )


def test_live_cli_emits_one_completed_five_minute_wait_and_closes_provider(
    tmp_path: Path, capsys: Any
) -> None:
    config = tmp_path / "signals.yaml"
    config.write_text("etfs: [510300]\n", encoding="utf-8")
    provider = _Provider(_events())

    status = main(
        ["live-signals", "--config", str(config), "--once"],
        push_provider=provider,
        as_of=datetime(2026, 11, 4, 10, 5, tzinfo=SHANGHAI),
    )

    output = json.loads(capsys.readouterr().out)
    assert status == 0
    result = output
    assert result["symbol"] == "510300"
    assert result["bar_timestamp"] == "2026-11-04T10:00:00+08:00"
    assert result["status"] == "WAIT"
    assert result["direction"] is None
    assert result["quantity"] is None
    assert "180" in " ".join(result["reasons"])
    assert result["parameters"] == TECHNICAL_PARAMETERS
    assert len(result["parameter_fingerprint"]) == 64
    assert provider.closed is True


def test_live_cli_deduplicates_same_rule_identity_and_timestamp(
    tmp_path: Path, capsys: Any
) -> None:
    config = tmp_path / "signals.yaml"
    config.write_text("etfs: [510300]\n", encoding="utf-8")
    provider = _Provider(_events() + _events())

    main(
        ["live-signals", "--config", str(config), "--once"],
        push_provider=provider,
        as_of=datetime(2026, 11, 4, 10, 5, tzinfo=SHANGHAI),
    )

    outputs = capsys.readouterr().out.splitlines()
    assert len(outputs) == 1
    assert json.loads(outputs[0])["symbol"] == "510300"


def test_live_cli_deduplicates_repeated_minute_before_aggregation(
    tmp_path: Path, capsys: Any
) -> None:
    config = tmp_path / "signals.yaml"
    config.write_text("etfs: [510300]\n", encoding="utf-8")
    bars = _events()
    provider = _Provider((bars[0], bars[1], bars[1], *bars[2:]))

    main(
        ["live-signals", "--config", str(config), "--once"],
        push_provider=provider,
        as_of=datetime(2026, 11, 4, 10, 5, tzinfo=SHANGHAI),
    )

    outputs = capsys.readouterr().out.splitlines()
    assert len(outputs) == 1
    assert json.loads(outputs[0])["status"] == "WAIT"


def test_live_output_deduplicates_by_symbol_not_only_timestamp(capsys: Any) -> None:
    from a500_t_trader.cli.live_signals import _emit_once

    seen: set[tuple[str, str]] = set()
    common = {
        "strategy_identity": "strategy:fingerprint",
        "bar_timestamp": "2026-11-04T10:00:00+08:00",
        "status": "WAIT",
    }
    _emit_once({**common, "symbol": "510300"}, seen)
    _emit_once({**common, "symbol": "510500"}, seen)

    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [item["symbol"] for item in outputs] == ["510300", "510500"]


def test_live_cli_reports_incomplete_five_minute_sequence_on_transition(
    tmp_path: Path, capsys: Any
) -> None:
    config = tmp_path / "signals.yaml"
    config.write_text("etfs: [510300]\n", encoding="utf-8")
    first = _events()
    next_bucket = MinuteBar(
        first[0].symbol,
        first[0].timestamp + timedelta(minutes=5),
        first[0].interval_minutes,
        first[0].open,
        first[0].high,
        first[0].low,
        first[0].close,
        first[0].volume,
        first[0].amount,
        True,
    )
    provider = _Provider((*first[:3], next_bucket))

    main(
        ["live-signals", "--config", str(config), "--once"],
        push_provider=provider,
        as_of=datetime(2026, 11, 4, 10, 6, tzinfo=SHANGHAI),
    )

    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert outputs
    assert outputs[0]["status"] == "UNAVAILABLE"
    assert "missing one-minute input" in outputs[0]["reasons"][0]


def test_live_cli_reports_missing_minute_as_directionless_unavailable(
    tmp_path: Path, capsys: Any
) -> None:
    config = tmp_path / "signals.yaml"
    config.write_text("etfs: [510300]\n", encoding="utf-8")
    next_bar = _events()[-1]
    gap_boundary = MinuteBar(
        next_bar.symbol,
        next_bar.timestamp + timedelta(minutes=1),
        next_bar.interval_minutes,
        next_bar.open,
        next_bar.high,
        next_bar.low,
        next_bar.close,
        next_bar.volume,
        next_bar.amount,
        True,
    )
    provider = _Provider(_events()[:4] + (gap_boundary,))

    status = main(
        ["live-signals", "--config", str(config), "--once"],
        push_provider=provider,
        as_of=datetime(2026, 11, 4, 10, 6, tzinfo=SHANGHAI),
    )

    result = json.loads(capsys.readouterr().out)
    assert status == 0
    assert result["status"] == "UNAVAILABLE"
    assert result["direction"] is None
    assert result["quantity"] is None
    assert "missing one-minute input" in result["reasons"][0]


def test_live_cli_reports_provider_failure_and_closes_provider(
    tmp_path: Path, capsys: Any
) -> None:
    class FailedProvider(_Provider):
        def subscribe(
            self, symbol: str, event_type: str, callback: object | None = None
        ) -> _Subscription:
            raise RuntimeError("push connection lost")

    config = tmp_path / "signals.yaml"
    config.write_text("etfs: [510300]\n", encoding="utf-8")
    provider = FailedProvider(())

    status = main(
        ["live-signals", "--config", str(config)],
        push_provider=provider,
    )

    error = json.loads(capsys.readouterr().err)
    assert status == 2
    assert error == {
        "error": "push connection lost",
        "status": "PROVIDER_ERROR",
    }
    assert provider.closed is True


def test_live_cli_uses_explicit_etf_scope_and_reports_stale_data(
    tmp_path: Path, capsys: Any
) -> None:
    config = tmp_path / "signals.yaml"
    config.write_text("etfs: [510300]\n", encoding="utf-8")
    provider = _Provider(_events("510300") + _events("510500"))

    main(
        ["live-signals", "--config", str(config), "--once"],
        push_provider=provider,
        as_of=datetime(2026, 11, 4, 11, 0, tzinfo=SHANGHAI),
    )

    result = json.loads(capsys.readouterr().out)
    assert result["symbol"] == "510300"
    assert result["status"] == "UNAVAILABLE"
    assert result["direction"] is None
    assert result["quantity"] is None
    assert any("stale" in reason for reason in result["reasons"])


def test_live_yaml_partial_overrides_have_stable_complete_fingerprint(
    tmp_path: Path,
) -> None:
    from a500_t_trader.strategy.intraday_v1_config import load_live_signal_config

    path = tmp_path / "signals.yaml"
    path.write_text(
        "etfs: [510300]\nparameters:\n  scores:\n    buy: 75\n",
        encoding="utf-8",
    )

    first = load_live_signal_config(path)
    second = load_live_signal_config(path)

    assert first.etfs == ("510300",)
    scores = first.parameters["scores"]
    assert isinstance(scores, Mapping)
    assert scores["buy"] == 75
    assert scores["sell"] == TECHNICAL_PARAMETERS["scores"]["sell"]
    assert first.fingerprint == second.fingerprint


def test_live_yaml_loads_account_costs_and_fingerprints_them(tmp_path: Path) -> None:
    from a500_t_trader.strategy.intraday_v1_config import load_live_signal_config

    path = tmp_path / "signals.yaml"
    path.write_text(
        "etfs: [510300]\ncosts:\n"
        "  510300:\n"
        "    tick_size: 0.001\n"
        "    commission_rate: 0.00005\n"
        "    minimum_commission: 0.1\n"
        "    reference_quantity: 40000\n",
        encoding="utf-8",
    )

    loaded = load_live_signal_config(path)

    assert loaded.costs["510300"] == {
        "tick_size": Decimal("0.001"),
        "commission_rate": Decimal("0.00005"),
        "minimum_commission": Decimal("0.1"),
        "reference_quantity": 40000,
    }

    original_fingerprint = loaded.fingerprint
    path.write_text(
        "etfs: [510300]\ncosts:\n"
        "  510300:\n"
        "    tick_size: 0.001\n"
        "    commission_rate: 0.00006\n"
        "    minimum_commission: 0.1\n"
        "    reference_quantity: 40000\n",
        encoding="utf-8",
    )
    assert load_live_signal_config(path).fingerprint != original_fingerprint


def test_live_cli_calculates_round_trip_commission_from_config(
    tmp_path: Path, capsys: Any
) -> None:
    config = tmp_path / "signals.yaml"
    config.write_text(
        "etfs: [510300]\ncosts:\n"
        "  510300:\n"
        "    tick_size: 0.001\n"
        "    commission_rate: 0.00005\n"
        "    minimum_commission: 0.1\n"
        "    reference_quantity: 40000\n",
        encoding="utf-8",
    )

    main(
        ["live-signals", "--config", str(config), "--once"],
        push_provider=_Provider(_events()),
        as_of=datetime(2026, 11, 4, 10, 5, tzinfo=SHANGHAI),
    )

    result = json.loads(capsys.readouterr().out)
    assert result["grid_context"] == {
        "tick_size": "0.001",
        "round_trip_cost_rate": "0.0001",
    }


def test_live_yaml_rejects_unknown_or_invalid_parameter(tmp_path: Path) -> None:
    import pytest

    from a500_t_trader.strategy.intraday_v1_config import load_live_signal_config

    path = tmp_path / "signals.yaml"
    for fragment in ("unknown: 1", "buy: nope", "buy: 1000"):
        path.write_text(
            f"etfs: [510300]\nparameters:\n  scores:\n    {fragment}\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            load_live_signal_config(path)


def test_live_yaml_validates_nested_timing_windows_and_rejects_short_etfs(
    tmp_path: Path,
) -> None:
    import pytest

    from a500_t_trader.strategy.intraday_v1_config import load_live_signal_config

    path = tmp_path / "signals.yaml"
    path.write_text(
        "etfs: [510300]\nparameters:\n  timing:\n"
        '    buy_sell_windows: [["09:55", "11:15"], ["13:00", "14:20"]]\n',
    )
    loaded = load_live_signal_config(path)
    timing = loaded.parameters["timing"]
    assert isinstance(timing, Mapping)
    assert timing["buy_sell_windows"] == [
        ["09:55", "11:15"],
        ["13:00", "14:20"],
    ]

    path.write_text("etfs: [300]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="six-digit ETF codes"):
        load_live_signal_config(path)

    path.write_text(
        "etfs: [510300]\nparameters:\n  timing:\n"
        "    buy_sell_windows: [[09:99, 11:15], [13:00, 14:20]]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="HH:MM"):
        load_live_signal_config(path)


def test_missing_live_yaml_uses_rule_defaults(tmp_path: Path) -> None:
    from a500_t_trader.strategy.intraday_v1_config import load_live_signal_config

    config = load_live_signal_config(tmp_path / "missing.yaml")

    assert config.parameters == TECHNICAL_PARAMETERS
