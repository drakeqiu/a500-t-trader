from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "start-local.py"
SPEC = importlib.util.spec_from_file_location("start_local", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
LOCAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LOCAL)


def test_startup_services_include_gateway_api_dependencies() -> None:
    services = LOCAL.SERVICES

    assert "jobs-api" in services
    assert services.index("jobs-api") < services.index("gateway")
