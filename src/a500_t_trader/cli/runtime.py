"""Safe configuration and startup for the AmazingData provider."""

from __future__ import annotations

import errno
import os
import socket
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

from ..market_data.providers.amazingdata import AmazingDataProvider

_REQUIRED_SETTINGS = (
    "AD_USERNAME",
    "AD_PASSWORD",
    "AD_HOST",
    "AD_PORT",
)
_TRANSIENT_ERRNOS = {
    errno.ECONNABORTED,
    errno.ECONNREFUSED,
    errno.ECONNRESET,
    errno.EHOSTUNREACH,
    errno.ENETDOWN,
    errno.ENETUNREACH,
    errno.ETIMEDOUT,
}


class ProviderStartupError(RuntimeError):
    """Base error for failed provider startup, without sensitive details."""


class ConfigurationError(ProviderStartupError):
    """Provider settings are missing or malformed."""


class MissingConfigurationError(ConfigurationError):
    """Required provider settings are absent."""


class AuthenticationError(ProviderStartupError):
    """Provider credentials were rejected."""


class PermissionDeniedError(ProviderStartupError):
    """Required local or remote permissions are unavailable."""


class RecoverableNetworkError(ProviderStartupError):
    """A transient network failure continued through bounded retries."""


def create_provider_from_env[ProviderT](
    *,
    env: Mapping[str, str] | None = None,
    env_file: str | Path = ".env",
    home: str | Path | None = None,
    provider_factory: Callable[..., ProviderT] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ProviderT:
    """Load credentials, prepare the SDK directory, and start the real provider."""
    factory = provider_factory or cast(Callable[..., ProviderT], AmazingDataProvider)
    process_env = os.environ if env is None else env
    settings = _read_dotenv(Path(env_file))
    settings.update(process_env)

    if any(not settings.get(key) for key in _REQUIRED_SETTINGS):
        raise MissingConfigurationError("Missing required AD provider configuration")
    username = settings["AD_USERNAME"]
    password = settings["AD_PASSWORD"]
    host = settings["AD_HOST"]
    try:
        port = int(settings["AD_PORT"])
    except ValueError:
        raise ConfigurationError("AD_PORT must be an integer") from None
    if not 1 <= port <= 65535:
        raise ConfigurationError("AD_PORT must be a valid TCP port")

    home_path = (
        Path(home) if home is not None else Path(settings.get("HOME") or Path.home())
    )
    _ensure_sdk_directory(home_path / "mdga_file")

    for attempt in range(3):
        try:
            return factory(username, password, host, port)
        except Exception as error:
            category = _error_category(error)
            if category == "network":
                if attempt < 2:
                    sleep(0.25 * (2**attempt))
                    continue
                raise RecoverableNetworkError(
                    "AmazingData provider startup failed after transient "
                    "network retries"
                ) from None
            if category == "authentication":
                raise AuthenticationError("AmazingData authentication failed") from None
            if category == "permission":
                raise PermissionDeniedError(
                    "AmazingData provider permission denied"
                ) from None
            if category == "configuration":
                raise ConfigurationError(
                    "AmazingData provider configuration failed"
                ) from None
            raise ProviderStartupError("AmazingData provider startup failed") from None
    raise AssertionError("bounded startup retry loop ended unexpectedly")


def _ensure_sdk_directory(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, probe = tempfile.mkstemp(prefix=".mdga-write-probe-", dir=directory)
        os.close(descriptor)
        try:
            os.unlink(probe)
        except OSError:
            raise PermissionDeniedError(
                "Cannot remove SDK directory write-probe file"
            ) from None
    except PermissionDeniedError:
        raise
    except OSError:
        raise PermissionDeniedError("SDK directory is not writable") from None


def _read_dotenv(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    except OSError:
        raise ConfigurationError("Cannot read .env configuration") from None
    values: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _exception_chain(error: Exception) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _error_category(error: Exception) -> str:
    chain = _exception_chain(error)
    message = " ".join(str(item).lower() for item in chain)
    if any(
        word in message
        for word in (
            "authentication",
            "unauthorized",
            "login was rejected",
            "invalid password",
            "bad credentials",
        )
    ):
        return "authentication"
    if isinstance(error, PermissionError) or any(
        word in message for word in ("permission", "forbidden", "access denied")
    ):
        return "permission"
    if any(
        isinstance(item, (TimeoutError, ConnectionError, socket.gaierror))
        or isinstance(item, OSError)
        and item.errno in _TRANSIENT_ERRNOS
        for item in chain
    ) or any(
        word in message
        for word in (
            "network unavailable",
            "connection timed out",
            "connection refused",
            "connection reset",
            "temporary failure",
            "temporary unavailable",
            "dns",
        )
    ):
        return "network"
    if isinstance(error, (KeyError, TypeError, ValueError)) or any(
        word in message
        for word in ("configuration", "invalid port", "missing required")
    ):
        return "configuration"
    return "unknown"
