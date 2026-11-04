"""Push-driven CLI workflow for completed-bar V1 signals."""

from __future__ import annotations

import json
import signal
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any, cast
from zoneinfo import ZoneInfo

from ..market_data import MinuteBar
from ..strategy.intraday_technical import analyze_intraday_technical
from ..strategy.intraday_v1_config import (
    RULE_SOURCE,
    RULE_SOURCE_SHA256,
    STRATEGY_ID,
    IntradayV1Config,
    LiveSignalConfig,
    load_live_signal_config,
)
from .common import json_default

SHANGHAI = ZoneInfo("Asia/Shanghai")


class LiveConfigurationError(ValueError):
    pass


def run_live_signals(
    provider: Any,
    *,
    config_path: str,
    once: bool = False,
    as_of: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Consume an injected push provider until interrupted or one bar is evaluated."""
    try:
        configuration = load_live_signal_config(Path(config_path))
    except Exception as error:
        print(
            json.dumps({"error": str(error), "status": "CONFIG_ERROR"}),
            file=sys.stderr,
        )
        return 2
    if not configuration.etfs:
        print(
            json.dumps(
                {
                    "error": "live YAML must explicitly list at least one ETF in etfs",
                    "status": "CONFIG_ERROR",
                }
            ),
            file=sys.stderr,
        )
        return 2
    now = clock or (lambda: datetime.now(SHANGHAI))
    events: Queue[MinuteBar | str | Exception] = Queue()
    subscriptions: list[Any] = []
    workers: list[Thread] = []
    buckets: dict[str, list[MinuteBar]] = defaultdict(list)
    histories: dict[str, list[MinuteBar]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    seen_minutes: set[tuple[str, str]] = set()
    seen_minute_days: dict[str, object] = {}
    stopped = Event()

    def subscribe(symbols: tuple[str, ...]) -> None:
        for subscription in subscriptions:
            subscription.close()
        subscriptions.clear()
        for symbol in symbols:
            subscription = provider.subscribe(symbol, "bar")
            subscriptions.append(subscription)
            worker = Thread(
                target=_pump_subscription,
                args=(subscription, events),
                daemon=True,
            )
            workers.append(worker)
            worker.start()

    previous_hup_handler = None
    try:
        if hasattr(signal, "SIGHUP"):
            previous_hup_handler = signal.getsignal(signal.SIGHUP)
            signal.signal(signal.SIGHUP, lambda _signum, _frame: events.put("reload"))
        subscribe(configuration.etfs)
        while not stopped.is_set():
            try:
                event = events.get(timeout=0.25)
            except Empty:
                continue
            if isinstance(event, Exception):
                raise event
            if event == "reload":
                try:
                    reloaded = load_live_signal_config(Path(config_path))
                except Exception as error:
                    raise LiveConfigurationError(str(error)) from None
                if not reloaded.etfs:
                    raise LiveConfigurationError(
                        "reloaded YAML must explicitly list at least one ETF in etfs"
                    )
                if reloaded.etfs != configuration.etfs:
                    subscribe(reloaded.etfs)
                    buckets.clear()
                    histories.clear()
                configuration = reloaded
                continue
            if not isinstance(event, MinuteBar):
                continue
            if event.symbol not in configuration.etfs or not event.is_completed:
                continue
            minute = event.timestamp.astimezone(SHANGHAI)
            if event.interval_minutes != 1:
                raise ValueError("live signal provider must emit one-minute bars")
            if seen_minute_days.get(event.symbol) != minute.date():
                seen_minutes = {
                    item for item in seen_minutes if item[0] != event.symbol
                }
                seen_minute_days[event.symbol] = minute.date()
            minute_key = (event.symbol, minute.isoformat())
            if minute_key in seen_minutes:
                continue
            seen_minutes.add(minute_key)
            bucket_start = minute.replace(
                minute=minute.minute - minute.minute % 5,
                second=0,
                microsecond=0,
            )
            bucket = buckets[event.symbol]
            if bucket and bucket[0].timestamp.astimezone(SHANGHAI) != bucket_start:
                prior_minute = bucket[0].timestamp.astimezone(SHANGHAI)
                prior_start = prior_minute.replace(
                    minute=prior_minute.minute - prior_minute.minute % 5,
                    second=0,
                    microsecond=0,
                )
                prior_expected = [
                    prior_start + timedelta(minutes=index) for index in range(5)
                ]
                prior_actual = [bar.timestamp.astimezone(SHANGHAI) for bar in bucket]
                if prior_actual != prior_expected:
                    payload = _unavailable(
                        event.symbol,
                        prior_start,
                        configuration,
                        "completed five-minute bar has missing one-minute input",
                    )
                    _emit_once(payload, seen)
                    if once:
                        stopped.set()
                bucket.clear()
            bucket.append(event)
            if minute.minute % 5 != 4:
                continue
            expected = [bucket_start + timedelta(minutes=index) for index in range(5)]
            actual = [bar.timestamp.astimezone(SHANGHAI) for bar in bucket]
            if len(bucket) != 5 or actual != expected:
                payload = _unavailable(
                    event.symbol,
                    bucket_start,
                    configuration,
                    "completed five-minute bar has missing one-minute input",
                )
                _emit_once(payload, seen)
                if once:
                    stopped.set()
                bucket.clear()
                continue
            five_minute = _aggregate_five_minute(bucket_start, bucket)
            bucket.clear()
            duplicate_key = (
                f"{STRATEGY_ID}:{configuration.fingerprint}:{event.symbol}",
                five_minute.timestamp.isoformat(),
            )
            if duplicate_key in seen:
                if once and events.empty():
                    stopped.set()
                continue
            current = (as_of or now()).astimezone(SHANGHAI)
            history = histories[event.symbol]
            payload = _evaluate(
                event.symbol, five_minute, history, configuration, current
            )
            if not history or history[-1].timestamp != five_minute.timestamp:
                history.append(five_minute)
                if len(history) > 240:
                    del history[:-240]
            _emit_once(payload, seen)
            if once:
                stopped.set()
    except KeyboardInterrupt:
        return 0
    except LiveConfigurationError as error:
        print(
            json.dumps({"error": str(error), "status": "CONFIG_ERROR"}),
            file=sys.stderr,
        )
        return 2
    except Exception as error:
        print(
            json.dumps({"error": str(error), "status": "PROVIDER_ERROR"}),
            file=sys.stderr,
        )
        return 2
    finally:
        if previous_hup_handler is not None:
            signal.signal(signal.SIGHUP, previous_hup_handler)
        for subscription in subscriptions:
            try:
                subscription.close()
            except Exception:
                pass
        try:
            provider.close()
        except Exception:
            pass
        for worker in workers:
            worker.join(timeout=1)
    return 0


def _pump_subscription(
    subscription: Any, events: Queue[MinuteBar | str | Exception]
) -> None:
    try:
        for event in subscription:
            if isinstance(event, MinuteBar):
                events.put(event)
    except Exception as error:
        events.put(error)


def _emit_once(payload: dict[str, object], seen: set[tuple[str, str]]) -> None:
    identity = str(payload["strategy_identity"])
    symbol = str(payload["symbol"])
    timestamp = str(payload["bar_timestamp"])
    key = f"{identity}:{symbol}", timestamp
    if key in seen:
        return
    seen.add(key)
    print(json.dumps(payload, ensure_ascii=False, default=json_default), flush=True)


def _evaluate(
    symbol: str,
    latest: MinuteBar,
    history: list[MinuteBar],
    configuration: LiveSignalConfig,
    as_of: datetime,
) -> dict[str, object]:
    identity = f"{STRATEGY_ID}:{configuration.fingerprint}"
    timestamp = latest.timestamp.astimezone(SHANGHAI)
    age = as_of - timestamp - timedelta(minutes=5)
    if age < timedelta(0) or age > timedelta(minutes=15):
        reason = "latest completed five-minute market data is stale or future-dated"
        return _unavailable(symbol, timestamp, configuration, reason)
    bars = tuple((*history, latest))
    v1 = IntradayV1Config(
        STRATEGY_ID,
        "V1.0",
        "live",
        RULE_SOURCE,
        RULE_SOURCE_SHA256,
        configuration.parameters,
        {},
        {},
        {},
        {},
        configuration.fingerprint,
    )
    cost_inputs = configuration.costs.get(symbol)
    grid_context: dict[str, Decimal] | None = None
    if cost_inputs is not None:
        tick_size = cast(Decimal, cost_inputs["tick_size"])
        commission_rate = cast(Decimal, cost_inputs["commission_rate"])
        minimum_commission = cast(Decimal, cost_inputs["minimum_commission"])
        reference_quantity = cast(int, cost_inputs["reference_quantity"])
        notional = latest.close * reference_quantity
        commission_per_side = max(notional * commission_rate, minimum_commission)
        grid_context = {
            "tick_size": tick_size,
            "round_trip_cost_rate": (
                Decimal(2) * commission_per_side / notional
            ).normalize(),
        }
    analysis = analyze_intraday_technical(
        {symbol: bars},
        config=v1,
        grid_context=None if grid_context is None else {symbol: grid_context},
        as_of=as_of,
    )[symbol]
    reasons = list(analysis.reasons)
    if analysis.completed_valid_bars < 180:
        status = "WAIT"
        direction = None
        quantity = None
        reasons.append(
            f"insufficient completed five-minute history: "
            f"{analysis.completed_valid_bars}/180"
        )
    elif grid_context is None:
        status = "UNAVAILABLE"
        direction = None
        quantity = None
        reasons.append(
            "missing commission cost configuration for this ETF; "
            "configure tick_size, commission_rate, minimum_commission, "
            "and reference_quantity"
        )
    elif analysis.candidate in {"BUY", "SELL"} and analysis.risk_status == "READY":
        status = analysis.candidate
        direction = analysis.candidate
        quantity = None
        reasons.append(
            "advisory signal only; quantity requires valid account, security "
            "rules, and execution inputs"
        )
    else:
        status = "WAIT"
        direction = None
        quantity = None
    return {
        "symbol": symbol,
        "bar_timestamp": timestamp.isoformat(),
        "strategy_identity": identity,
        "rule_version": "V1.0",
        "parameters": configuration.parameters,
        "parameter_fingerprint": configuration.fingerprint,
        "status": status,
        "direction": direction,
        "quantity": quantity,
        "grid_context": grid_context,
        "reasons": list(dict.fromkeys(reasons)),
    }


def _unavailable(
    symbol: str,
    timestamp: datetime,
    configuration: LiveSignalConfig,
    reason: str,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "bar_timestamp": timestamp.isoformat(),
        "strategy_identity": f"{STRATEGY_ID}:{configuration.fingerprint}",
        "rule_version": "V1.0",
        "parameters": configuration.parameters,
        "parameter_fingerprint": configuration.fingerprint,
        "status": "UNAVAILABLE",
        "direction": None,
        "quantity": None,
        "reasons": [reason],
    }


def _aggregate_five_minute(start: datetime, bars: list[MinuteBar]) -> MinuteBar:
    return MinuteBar(
        bars[0].symbol,
        start,
        5,
        bars[0].open,
        max(bar.high for bar in bars),
        min(bar.low for bar in bars),
        bars[-1].close,
        sum(bar.volume for bar in bars),
        None
        if any(bar.amount is None for bar in bars)
        else sum((bar.amount for bar in bars if bar.amount is not None), Decimal(0)),
        True,
        source=bars[-1].source,
    )
