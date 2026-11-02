from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


from a500_t_trader.strategy.intraday_execution import calculate_intraday_execution
from a500_t_trader.strategy.intraday_technical import IntradayTechnicalAnalysis
from a500_t_trader.strategy.intraday_v1_config import load_intraday_v1_config
from test_intraday_v1_config import _complete_config, _write_config

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_execution_reports_explicit_nav_and_blocks_unknown_candidate(
    tmp_path: Path,
) -> None:
    config = load_intraday_v1_config(
        _write_config(tmp_path / "rules.json", _complete_config())
    )
    analysis = IntradayTechnicalAnalysis(
        symbol="510300",
        trend="RANGE",
        indicators={"close": Decimal("4.00")},
        scores={"buy": None, "sell": None},
        candidate="WAIT",
        risk_status="BLOCKED",
        reasons=("exact V1.0 scoring rules are unavailable",),
        completed_valid_bars=180,
    )

    result = calculate_intraday_execution(
        analyses={"510300": analysis},
        config=config,
        as_of=datetime(2026, 11, 2, 10, 0, tzinfo=SHANGHAI),
        liquidity={},
    )

    assert result.nav == Decimal("54000")
    assert result.symbols["510300"].allowed is False
    assert result.symbols["510300"].quantity == 0
    assert "candidate is not actionable" in result.symbols["510300"].reasons


def test_missing_position_valuation_blocks_all_action_quantities(
    tmp_path: Path,
) -> None:
    config = load_intraday_v1_config(
        _write_config(tmp_path / "rules.json", _complete_config())
    )
    account = dict(config.account)
    positions = dict(account["positions"])
    position = dict(positions["510300"])
    position["market_value"] = None
    positions["510300"] = position
    account["positions"] = positions
    config = replace(config, account=account)
    analysis = IntradayTechnicalAnalysis(
        symbol="510300", trend="RANGE", indicators={}, scores={"buy": 80, "sell": 0},
        candidate="BUY", risk_status="READY", reasons=(), completed_valid_bars=180,
    )

    result = calculate_intraday_execution(
        analyses={"510300": analysis},
        config=config,
        as_of=datetime(2026, 11, 2, 10, 0, tzinfo=SHANGHAI),
        liquidity={},
    )

    assert result.nav is None
    assert result.symbols["510300"].quantity == 0
    assert result.symbols["510300"].allowed is False
    assert any("market_value" in reason for reason in result.symbols["510300"].reasons)

