from __future__ import annotations

import importlib
from types import ModuleType

import pytest

from a500_t_trader.cli.runtime import (
    AuthenticationError,
    ConfigurationError,
    PermissionDeniedError,
    ProviderStartupError,
    RecoverableNetworkError,
)


def _account_api(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    snapshot_api = importlib.import_module("a500_t_trader.account_snapshot_api")
    monkeypatch.setenv("AD_USERNAME", "acceptance")
    monkeypatch.setenv("AD_PASSWORD", "configured")
    monkeypatch.setenv("AD_HOST", "provider.example.test")
    monkeypatch.setenv("AD_PORT", "443")
    monkeypatch.setattr(snapshot_api, "create_app", lambda **_kwargs: object())
    runtime = importlib.import_module("a500_t_trader.cli.runtime")
    monkeypatch.setattr(runtime, "create_provider_from_env", lambda **_kwargs: object())
    return importlib.import_module("a500_t_trader.account_api")


def test_account_api_starts_configured_provider_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_api = _account_api(monkeypatch)
    started: list[bool] = []

    def start_provider(**_kwargs: object) -> object:
        started.append(True)
        return object()

    monkeypatch.setattr(account_api, "create_provider_from_env", start_provider)

    assert account_api.create_market_data_service() is not None
    assert started == [True]


@pytest.mark.parametrize(
    "startup_error",
    [
        ConfigurationError("missing settings"),
        AuthenticationError("credentials rejected"),
        PermissionDeniedError("permission denied"),
        RecoverableNetworkError("provider unavailable"),
        ProviderStartupError("provider failed"),
    ],
)
def test_provider_startup_failure_is_not_suppressed(
    monkeypatch: pytest.MonkeyPatch,
    startup_error: ProviderStartupError,
) -> None:
    account_api = _account_api(monkeypatch)

    def fail_startup(**_kwargs: object) -> object:
        raise startup_error

    monkeypatch.setattr(account_api, "create_provider_from_env", fail_startup)

    with pytest.raises(type(startup_error), match=str(startup_error)):
        account_api.create_market_data_service()
