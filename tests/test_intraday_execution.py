from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from test_intraday_v1_config import _complete_config, _write_config

from a500_t_trader.strategy.intraday_execution import calculate_intraday_execution
from a500_t_trader.strategy.intraday_technical import IntradayTechnicalAnalysis
from a500_t_trader.strategy.intraday_v1_config import load_intraday_v1_config

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
        symbol="510300",
        trend="RANGE",
        indicators={},
        scores={"buy": 80, "sell": 0},
        candidate="BUY",
        risk_status="READY",
        reasons=(),
        completed_valid_bars=180,
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


def test_sell_quantity_is_capped_at_eight_percent_of_nav(tmp_path: Path) -> None:
    config_data = _complete_config()
    account = config_data["account"]
    assert isinstance(account, dict)
    account["positions"]["510300"].update(
        {
            "quantity": 10000,
            "protected_floor": 0,
            "sellable_quantity": 10000,
            "market_value": "40000",
        }
    )
    config_data["parameters"]["signals"]["sell_threshold"] = 68
    config_data["parameters"]["risk"].update(
        {
            "daily_turnover_limit": "100000",
            "daily_sell_limit": "100000",
            "max_daily_additions": "3",
            "max_daily_fills": "8",
        }
    )
    config = load_intraday_v1_config(
        _write_config(tmp_path / "rules.json", config_data)
    )
    analysis = IntradayTechnicalAnalysis(
        symbol="510300",
        trend="RANGE",
        indicators={"close": Decimal("4.000")},
        scores={"buy": 0, "sell": 80},
        candidate="SELL",
        risk_status="READY",
        reasons=(),
        completed_valid_bars=180,
    )

    result = calculate_intraday_execution(
        analyses={"510300": analysis},
        config=config,
        as_of=datetime(2026, 11, 2, 10, 0, tzinfo=SHANGHAI),
        liquidity={
            "510300": {
                "as_of": "2026-11-02T09:59:00+08:00",
                "average_daily_amount": "30000000",
                "latest_bar_amount": "1000000",
                "latest_bar_volume": "100000",
                "spread": "0.001",
                "atr": "0.01",
                "grid_step": "0.01",
            }
        },
        certification_passed=True,
        daily_activity={
            "turnover_notional": "0",
            "sell_notional": "0",
            "additions": 0,
            "fills": 0,
        },
    )

    assert result.symbols["510300"].allowed is True
    assert result.symbols["510300"].quantity == 1800


def test_buy_quantity_respects_remaining_daily_turnover_budget(tmp_path: Path) -> None:
    config_data = _complete_config()
    parameters = config_data["parameters"]
    assert isinstance(parameters, dict)
    risk = parameters["risk"]
    assert isinstance(risk, dict)
    risk.update(
        {
            "daily_turnover_limit": "81000",
            "daily_sell_limit": "5400",
            "max_daily_additions": "3",
            "max_daily_fills": "8",
        }
    )
    config = load_intraday_v1_config(
        _write_config(tmp_path / "rules.json", config_data)
    )
    analysis = IntradayTechnicalAnalysis(
        symbol="510300",
        trend="RANGE",
        indicators={"close": Decimal("4.000")},
        scores={"buy": 80, "sell": 0},
        candidate="BUY",
        risk_status="READY",
        reasons=(),
        completed_valid_bars=180,
    )

    result = calculate_intraday_execution(
        analyses={"510300": analysis},
        config=config,
        as_of=datetime(2026, 11, 2, 10, 0, tzinfo=SHANGHAI),
        liquidity={
            "510300": {
                "as_of": "2026-11-02T09:59:00+08:00",
                "average_daily_amount": "30000000",
                "latest_bar_amount": "1000000",
                "latest_bar_volume": "100000",
                "spread": "0.001",
                "atr": "0.01",
                "grid_step": "0.01",
            }
        },
        certification_passed=True,
        daily_activity={
            "turnover_notional": "80000",
            "sell_notional": "0",
            "additions": 0,
            "fills": 0,
        },
    )

    assert result.symbols["510300"].allowed is True
    assert result.symbols["510300"].quantity == 200
