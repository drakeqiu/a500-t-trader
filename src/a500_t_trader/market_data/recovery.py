"""Volatile bounded recovery for latest-state market-data events."""

from __future__ import annotations

from . import MinuteBar, Snapshot
from .collector import Publisher
from .events import MarketEvent


class LatestStatePublisher:
    """Retain only latest snapshot/bar per configured symbol during publish failures.

    Trades are never retained. State is memory-only and scoped to this object;
    successful broker publication is at least once, so repeats are possible.
    Consumers, not this adapter, determine whether event timestamps are fresh.
    """

    def __init__(self, publisher: Publisher, *, symbols: tuple[str, ...]) -> None:
        if not symbols:
            raise ValueError("at least one configured symbol is required")
        self._publisher = publisher
        self._symbols = frozenset(symbols)
        self._pending: dict[tuple[str, str], Snapshot | MinuteBar] = {}

    @property
    def pending_count(self) -> int:
        """Number of volatile pending states (at most two per symbol)."""
        return len(self._pending)

    def publish(self, event: MarketEvent) -> None:
        if event.symbol not in self._symbols:
            raise ValueError(f"event symbol is not configured: {event.symbol}")
        key = self._state_key(event)
        if key is not None:
            # A new state supersedes a stale pending value of the same kind.
            self._pending.pop(key, None)
        self._flush()
        if self._pending:
            self._retain(event)
            return
        try:
            self._publisher.publish(event)
        except Exception:
            self._retain(event)

    @staticmethod
    def _state_key(event: MarketEvent) -> tuple[str, str] | None:
        if isinstance(event, Snapshot):
            return "snapshot", event.symbol
        if isinstance(event, MinuteBar):
            return "minute_bar", event.symbol
        return None

    def _retain(self, event: MarketEvent) -> None:
        if isinstance(event, (Snapshot, MinuteBar)):
            event_type = "snapshot" if isinstance(event, Snapshot) else "minute_bar"
            self._pending[(event_type, event.symbol)] = event

    def _flush(self) -> None:
        for key, event in tuple(self._pending.items()):
            try:
                self._publisher.publish(event)
            except Exception:
                return
            del self._pending[key]
