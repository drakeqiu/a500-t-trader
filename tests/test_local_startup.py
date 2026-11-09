from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "start-local.py"
SPEC = importlib.util.spec_from_file_location("start_local", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
LOCAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LOCAL)


def test_startup_services_include_history_worker_and_dispatcher() -> None:
    services = LOCAL.SERVICES

    assert "jobs-api" in services
    assert "jobs-worker" in services
    assert "history-dispatcher" in services
    assert services.index("jobs-api") < services.index("gateway")


def test_local_environment_keeps_provider_configuration_ad_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        "AD_USERNAME=trader\nAD_PASSWORD=secret\nAD_HOST=market.example\nAD_PORT=9876\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(LOCAL, "ROOT", tmp_path)
    for name in ("USERNAME", "PASSWORD", "HOST", "PORT"):
        monkeypatch.delenv(f"AD_{name}", raising=False)
        monkeypatch.delenv(f"AMAZINGDATA_{name}", raising=False)

    environment = LOCAL.local_environment()

    assert environment["AD_USERNAME"] == "trader"
    assert environment["AD_PORT"] == "9876"
    assert not any(name.startswith("AMAZINGDATA_") for name in environment)


def test_local_environment_enables_history_persistence_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(LOCAL, "ROOT", tmp_path)
    monkeypatch.delenv("HISTORY_PERSISTENCE_PERMITTED", raising=False)

    assert LOCAL.local_environment()["HISTORY_PERSISTENCE_PERMITTED"] == "true"


def test_local_environment_preserves_explicit_persistence_opt_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(LOCAL, "ROOT", tmp_path)
    monkeypatch.setenv("HISTORY_PERSISTENCE_PERMITTED", "false")

    assert LOCAL.local_environment()["HISTORY_PERSISTENCE_PERMITTED"] == "false"


def test_local_environment_defaults_build_mirrors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UV_INDEX_URL", raising=False)
    monkeypatch.delenv("APT_MIRROR", raising=False)
    monkeypatch.delenv("UV_HTTP_TIMEOUT", raising=False)

    environment = LOCAL.local_environment()

    assert environment["UV_INDEX_URL"] == "https://mirrors.aliyun.com/pypi/simple/"
    assert environment["UV_HTTP_TIMEOUT"] == "180"
    assert environment["APT_MIRROR"] == "mirrors.bfsu.edu.cn"


def test_local_environment_defaults_empty_build_mirrors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UV_INDEX_URL", "")
    monkeypatch.setenv("APT_MIRROR", "")
    monkeypatch.setenv("UV_HTTP_TIMEOUT", "")

    environment = LOCAL.local_environment()

    assert environment["UV_INDEX_URL"] == "https://mirrors.aliyun.com/pypi/simple/"
    assert environment["UV_HTTP_TIMEOUT"] == "180"
    assert environment["APT_MIRROR"] == "mirrors.bfsu.edu.cn"


def test_local_environment_preserves_configured_build_mirrors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UV_INDEX_URL", "https://pypi.example/simple/")
    monkeypatch.setenv("APT_MIRROR", "apt.example")
    monkeypatch.setenv("UV_HTTP_TIMEOUT", "90")
    environment = LOCAL.local_environment()

    assert environment["UV_INDEX_URL"] == "https://pypi.example/simple/"
    assert environment["UV_HTTP_TIMEOUT"] == "90"
    assert environment["APT_MIRROR"] == "apt.example"
