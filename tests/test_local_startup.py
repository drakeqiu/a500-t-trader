from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "start-local.py"
SPEC = importlib.util.spec_from_file_location("start_local", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
LOCAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LOCAL)


def test_startup_services_include_gateway_api_dependencies() -> None:
    services = LOCAL.SERVICES

    assert "jobs-api" in services
    assert services.index("jobs-api") < services.index("gateway")


def test_local_environment_defaults_build_mirrors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UV_INDEX_URL", raising=False)
    monkeypatch.delenv("APT_MIRROR", raising=False)
    monkeypatch.delenv("UV_HTTP_TIMEOUT", raising=False)

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
