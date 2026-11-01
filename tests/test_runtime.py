from pathlib import Path

import pytest

from a500_t_trader.cli.runtime import (
    AuthenticationError,
    ConfigurationError,
    PermissionDeniedError,
    RecoverableNetworkError,
    create_provider_from_env,
)


def _config() -> dict[str, str]:
    return {
        "AMAZINGDATA_USERNAME": "trader",
        "AMAZINGDATA_PASSWORD": "top-secret",
        "AMAZINGDATA_HOST": "market.example",
        "AMAZINGDATA_PORT": "9876",
    }


def test_loads_existing_env_file_and_prepares_mdga_directory_before_sdk(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AMAZINGDATA_USERNAME=trader\n"
        "AMAZINGDATA_PASSWORD='top-secret'\n"
        "AMAZINGDATA_HOST=market.example\n"
        "AMAZINGDATA_PORT=9876\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    events: list[tuple[str, object]] = []

    def sdk_factory(username: str, password: str, host: str, port: int) -> object:
        directory = home / "mdga_file"
        events.append(("sdk", (username, password, host, port)))
        assert directory.is_dir()
        assert list(directory.iterdir()) == []
        return object()

    result = create_provider_from_env(
        env={}, env_file=env_file, home=home, provider_factory=sdk_factory
    )

    assert events == [("sdk", ("trader", "top-secret", "market.example", 9876))]
    assert result is not None


def test_environment_overrides_env_file_without_exposing_credentials(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AMAZINGDATA_PASSWORD=file-secret\n", encoding="utf-8")
    values = _config()
    values["AMAZINGDATA_PASSWORD"] = "environment-secret"
    calls: list[str] = []

    create_provider_from_env(
        env=values,
        env_file=env_file,
        home=tmp_path / "home",
        provider_factory=lambda username, password, host, port: calls.append(password),
    )

    assert calls == ["environment-secret"]


def test_invalid_configuration_is_categorized_before_sdk_initialization(
    tmp_path: Path,
) -> None:
    values = _config()
    del values["AMAZINGDATA_PORT"]
    calls = 0

    def sdk_factory(*_args: object) -> object:
        nonlocal calls
        calls += 1
        return object()

    with pytest.raises(ConfigurationError):
        create_provider_from_env(
            env=values, home=tmp_path / "home", provider_factory=sdk_factory
        )

    assert calls == 0


def test_mdga_directory_permission_failure_prevents_sdk_start(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    blocked = home / "mdga_file"
    blocked.write_text("not a directory", encoding="utf-8")
    calls = 0

    def sdk_factory(*_args: object) -> object:
        nonlocal calls
        calls += 1
        return object()

    with pytest.raises(PermissionDeniedError):
        create_provider_from_env(env=_config(), home=home, provider_factory=sdk_factory)

    assert calls == 0


def test_authentication_and_permission_errors_are_not_retried(tmp_path: Path) -> None:
    for error, error_type in (
        (RuntimeError("login was rejected"), AuthenticationError),
        (PermissionError("permission denied"), PermissionDeniedError),
    ):
        calls = 0

        def sdk_factory(*_args: object, failure: Exception = error) -> object:
            nonlocal calls
            calls += 1
            raise failure

        with pytest.raises(error_type):
            create_provider_from_env(
                env=_config(),
                home=tmp_path / error_type.__name__,
                provider_factory=sdk_factory,
                sleep=lambda _seconds: None,
            )
        assert calls == 1


def test_only_transient_network_errors_get_bounded_retries(tmp_path: Path) -> None:
    calls = 0
    delays: list[float] = []
    provider = object()

    def sdk_factory(*_args: object) -> object:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError("connection timed out")
        return provider

    actual = create_provider_from_env(
        env=_config(),
        home=tmp_path / "home",
        provider_factory=sdk_factory,
        sleep=delays.append,
    )

    assert actual is provider
    assert calls == 3
    assert delays == [0.25, 0.5]


def test_exhausted_network_retry_is_categorized_and_never_falls_back(
    tmp_path: Path,
) -> None:
    calls = 0

    def sdk_factory(*_args: object) -> object:
        nonlocal calls
        calls += 1
        raise ConnectionError("network unavailable")

    with pytest.raises(RecoverableNetworkError):
        create_provider_from_env(
            env=_config(),
            home=tmp_path / "home",
            provider_factory=sdk_factory,
            sleep=lambda _seconds: None,
        )

    assert calls == 3


def test_failure_message_never_contains_credentials(tmp_path: Path) -> None:
    values = _config()

    def sdk_factory(*_args: object) -> object:
        raise RuntimeError("bad credentials: top-secret")

    with pytest.raises(AuthenticationError) as caught:
        create_provider_from_env(
            env=values, home=tmp_path / "home", provider_factory=sdk_factory
        )

    assert "top-secret" not in str(caught.value)
