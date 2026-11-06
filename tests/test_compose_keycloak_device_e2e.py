from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "compose-keycloak-device-e2e.py"
SPEC = importlib.util.spec_from_file_location("compose_keycloak_device_e2e", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
E2E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E2E)


@pytest.mark.parametrize(
    ("compose_env", "expected_prefix"),
    [
        ("podman-compose", ["podman-compose"]),
        (None, ["docker", "compose"]),
    ],
)
def test_compose_startup_builds_api_services_and_cleans_only_its_project(
    monkeypatch: pytest.MonkeyPatch,
    compose_env: str | None,
    expected_prefix: list[str],
) -> None:
    if compose_env is None:
        monkeypatch.delenv("PODMAN_COMPOSE", raising=False)
        monkeypatch.delenv("A500_COMPOSE_COMMAND", raising=False)
    else:
        monkeypatch.setenv("PODMAN_COMPOSE", compose_env)
        monkeypatch.delenv("A500_COMPOSE_COMMAND", raising=False)

    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(E2E, "_available_port", iter([8081, 8082]).__next__)
    monkeypatch.setattr(
        E2E.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(E2E, "_wait_for_keycloak", lambda _url: None)
    monkeypatch.setattr(E2E, "_admin_token", lambda *_args: "admin-token")
    monkeypatch.setattr(E2E, "_allow_browser_origin", lambda *_args: None)
    monkeypatch.setattr(E2E, "_check_browser_login", lambda *_args: None)
    monkeypatch.setattr(E2E, "_create_temporary_user", lambda *_args: None)
    monkeypatch.setattr(E2E, "_device_authorize", lambda *_args: "access-token")
    monkeypatch.setattr(E2E, "_check_api", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(E2E, "_verify_user_persisted", lambda *_args: None)

    E2E.main()

    startup, cleanup = calls[0][0], calls[-1][0]
    assert startup[: len(expected_prefix)] == expected_prefix
    assert str(E2E.ROOT / "compose.web.yaml") in startup
    assert startup[startup.index("up") + 1 :] == [
        "-d",
        "--build",
        "keycloak",
        "signal-api",
        "account-api",
        "gateway",
    ]
    assert cleanup[cleanup.index("down") + 1 :] == [
        "--volumes",
        "--remove-orphans",
    ]
    project_flag = "--project-name"
    assert (
        startup[startup.index(project_flag) + 1]
        == cleanup[cleanup.index(project_flag) + 1]
    )
    assert startup[startup.index(project_flag) + 1].startswith("a500-keycloak-e2e-")
