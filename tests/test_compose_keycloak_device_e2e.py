from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "compose-keycloak-device-e2e.py"
SPEC = importlib.util.spec_from_file_location("compose_keycloak_device_e2e", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
E2E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E2E)


@pytest.mark.parametrize(
    ("compose_env", "expected_prefix"),
    [("podman-compose", ["podman-compose"]), (None, ["docker", "compose"])],
)
def test_acceptance_runs_both_modes_and_cleans_its_project(
    monkeypatch: pytest.MonkeyPatch, compose_env: str | None, expected_prefix: list[str]
) -> None:
    monkeypatch.delenv("A500_COMPOSE_COMMAND", raising=False)
    if compose_env is None:
        monkeypatch.delenv("PODMAN_COMPOSE", raising=False)
    else:
        monkeypatch.setenv("PODMAN_COMPOSE", compose_env)
    # Inherited deployment settings must not invalidate default-listener coverage.
    monkeypatch.setenv("A500_GATEWAY_HOST", "192.0.2.1")
    monkeypatch.setenv("A500_GATEWAY_HOST_PORT", "9999")
    calls: list[tuple[list[str], dict[str, Any]]] = []
    bindings: list[tuple[str, int]] = []
    browser: list[tuple[str, bool]] = []
    api: list[tuple[str, bool]] = []
    plaintext: list[int] = []
    monkeypatch.setattr(E2E, "_available_port", iter([8081, 18080, 18443]).__next__)
    monkeypatch.setattr(E2E, "_make_test_certificate", lambda d: (d / "cert.pem", d / "key.pem"))
    monkeypatch.setattr(E2E.ssl, "create_default_context", lambda **_kwargs: object())
    def run(args: list[str], **kwargs: Any) -> None:
        calls.append((args, {**kwargs, "env": dict(kwargs["env"])}))
    monkeypatch.setattr(E2E.subprocess, "run", run)
    monkeypatch.setattr(E2E, "_wait_for_keycloak", lambda _url: None)
    monkeypatch.setattr(E2E, "_admin_token", lambda *_args: "admin-token")
    monkeypatch.setattr(E2E, "_allow_browser_origin", lambda *_args: None)
    monkeypatch.setattr(E2E, "_check_browser_login", lambda url, *_args, https: browser.append((url, https)))
    monkeypatch.setattr(E2E, "_create_temporary_user", lambda *_args: None)
    monkeypatch.setattr(E2E, "_device_authorize", lambda *_args: "access-token")
    monkeypatch.setattr(E2E, "_check_api", lambda url, *_args, create, **_kwargs: api.append((url, create)))
    monkeypatch.setattr(E2E, "_check_binding", lambda _c, _e, host, port: bindings.append((host, port)))
    monkeypatch.setattr(E2E, "_check_no_plaintext", plaintext.append)
    monkeypatch.setattr(E2E, "_verify_user_persisted", lambda *_args: None)

    E2E.main()

    startup, cleanup = calls[0][0], calls[-1][0]
    assert startup[: len(expected_prefix)] == expected_prefix
    assert str(E2E.ROOT / "compose.web.yaml") in startup
    assert str(E2E.ROOT / "compose.web-http.yaml") in startup
    assert startup[startup.index("up") + 1 :] == [
        "-d", "--build", "keycloak", "signal-api", "account-api", "gateway",
    ]
    assert "A500_GATEWAY_HOST" not in calls[0][1]["env"]
    assert "A500_GATEWAY_HOST_PORT" not in calls[0][1]["env"]
    assert bindings == [("127.0.0.1", 8080), ("0.0.0.0", 18080), ("127.0.0.1", 8443), ("0.0.0.0", 18443)]
    assert browser == [("http://localhost:8080", False), ("http://localhost:18080", False), ("https://localhost:8443", True), ("https://localhost:18443", True)]
    assert api == [(url, index == 0) for index, (url, _) in enumerate(browser)] + [("https://localhost:18443", False)]
    assert plaintext == [8443, 18443]
    recreates = [(args, options["env"]) for args, options in calls if "--force-recreate" in args]
    assert len(recreates) == 3
    assert str(E2E.ROOT / "compose.web-https.yaml") in recreates[1][0]
    assert "A500_GATEWAY_HOST" not in recreates[1][1]
    assert recreates[2][1]["A500_GATEWAY_HOST"] == "0.0.0.0"
    assert recreates[2][1]["A500_GATEWAY_HOST_PORT"] == "18443"
    assert cleanup[cleanup.index("down") + 1 :] == ["--volumes", "--remove-orphans"]
    project = startup[startup.index("--project-name") + 1]
    assert project.startswith("a500-keycloak-e2e-")
    assert cleanup[cleanup.index("--project-name") + 1] == project
    assert E2E.TLS_CONTEXT is None


def test_binding_checks_runtime_published_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = iter(["127.0.0.1:8080\n", "", "", ""])
    monkeypatch.setattr(E2E.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=next(outputs)))
    E2E._check_binding(["compose"], {}, "127.0.0.1", 8080)
    outputs = iter(["0.0.0.0:8080\n"])
    with pytest.raises(AssertionError, match="监听地址"):
        E2E._check_binding(["compose"], {}, "127.0.0.1", 8080)
    outputs = iter(["127.0.0.1:8080\n", "0.0.0.0:3000\n"])
    with pytest.raises(AssertionError, match="frontend"):
        E2E._check_binding(["compose"], {}, "127.0.0.1", 8080)


def test_https_rejects_plaintext_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(E2E, "_request", lambda _url: (400, "bad request"))
    E2E._check_no_plaintext(8443)
    monkeypatch.setattr(E2E, "_request", lambda _url: (401, "unauthorized"))
    with pytest.raises(AssertionError, match="明文"):
        E2E._check_no_plaintext(8443)


def test_https_requests_use_certificate_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    context = object()
    monkeypatch.setattr(E2E, "TLS_CONTEXT", context)

    def reject_untrusted(_request: Any, *, timeout: int, context: Any) -> None:
        assert context is E2E.TLS_CONTEXT
        raise E2E.URLError("certificate verify failed")

    monkeypatch.setattr(E2E, "urlopen", reject_untrusted)
    with pytest.raises(E2E.URLError, match="certificate verify failed"):
        E2E._request("https://localhost:8443/")
