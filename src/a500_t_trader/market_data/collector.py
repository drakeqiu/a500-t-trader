"""Standalone tgw push collector lifecycle and recovery state."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Event
from typing import Any, Literal, Protocol

from .events import MarketEvent

ConnectionState = Literal["disconnected", "connected", "stopped"]
SubscriptionState = Literal["idle", "subscribed", "failed", "stopped"]

StatusCallback = Callable[[ConnectionState, SubscriptionState], None]


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    symbols: tuple[str, ...]
    retry_initial_seconds: float = 1.0
    retry_max_seconds: float = 30.0
    poll_interval_seconds: float = 0.25
    connection_options: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("at least one symbol is required")
        if (
            self.retry_initial_seconds <= 0
            or self.retry_max_seconds < self.retry_initial_seconds
        ):
            raise ValueError("invalid retry interval")
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll interval must be positive")


class PushConnection(Protocol):
    connected: bool

    def subscribe(
        self, symbol: str, event_type: str, callback: Callable[[MarketEvent], None]
    ) -> Any: ...

    def close(self) -> None: ...


class Publisher(Protocol):
    def publish(self, event: MarketEvent) -> None: ...


ConnectionFactory = Callable[[CollectorConfig], PushConnection]


class MarketDataCollector:
    """Own push connections and subscriptions, publishing normalized events."""

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        config: CollectorConfig,
        publisher: Publisher,
        *,
        wait: Callable[[Event, float], bool] = lambda stop, seconds: stop.wait(seconds),
        on_status: StatusCallback | None = None,
    ) -> None:
        self._connection_factory = connection_factory
        self._config = config
        self._publisher = publisher
        self._wait = wait
        self._on_status = on_status
        self.connection_state: ConnectionState = "disconnected"
        self.subscription_state: SubscriptionState = "idle"
        self.last_error: str | None = None

    def run(self, stop: Event) -> None:
        """Connect, subscribe, and recover until the caller requests shutdown."""
        retry_delay = self._config.retry_initial_seconds
        while not stop.is_set():
            connection: PushConnection | None = None
            subscriptions: list[Any] = []
            try:
                connection = self._connection_factory(self._config)
                if not _is_connected(connection):
                    raise ConnectionError(
                        "connection factory returned without confirmed connection"
                    )
                self._set_status("connected", "idle")
                self.last_error = None
                for symbol in self._config.symbols:
                    for event_type in ("snapshot", "bar", "tick"):
                        subscriptions.append(
                            connection.subscribe(
                                symbol,
                                event_type,
                                self._publish,
                            )
                        )
                self._set_status("connected", "subscribed")
                while not stop.is_set():
                    if not _is_connected(connection):
                        self._set_status("disconnected", "failed")
                        self.last_error = "tgw connection reported disconnected"
                        break
                    failed = next(
                        (
                            error
                            for subscription in subscriptions
                            if (error := getattr(subscription, "error", None))
                        ),
                        None,
                    )
                    if failed is not None:
                        self._set_status(self.connection_state, "failed")
                        self.last_error = str(failed)
                        break
                    if self._wait(stop, self._config.poll_interval_seconds):
                        break
            except Exception as error:
                self.last_error = str(error)
                connection_state: ConnectionState = (
                    "disconnected"
                    if connection is None or not _is_connected(connection)
                    else "connected"
                )
                self._set_status(connection_state, "failed")
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception as error:
                        if self.last_error is None:
                            self.last_error = str(error)
                if not stop.is_set():
                    self._set_status("disconnected", self.subscription_state)
                    self._wait(stop, retry_delay)
                    retry_delay = min(retry_delay * 2, self._config.retry_max_seconds)
        self._set_status("stopped", "stopped")

    def _set_status(
        self, connection: ConnectionState, subscription: SubscriptionState
    ) -> None:
        changed = (
            connection != self.connection_state
            or subscription != self.subscription_state
        )
        self.connection_state = connection
        self.subscription_state = subscription
        if changed and self._on_status is not None:
            self._on_status(connection, subscription)

    def _publish(self, event: MarketEvent) -> None:
        self._publisher.publish(event)


def _is_connected(connection: PushConnection) -> bool:
    state = getattr(connection, "is_connected", None)
    if state is None:
        state = getattr(connection, "connected", None)
    return bool(state() if callable(state) else state)
