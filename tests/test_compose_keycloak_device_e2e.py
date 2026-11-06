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



def test_main_requires_real_provider_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("USERNAME", "PASSWORD", "HOST", "PORT"):
        monkeypatch.delenv(f"AMAZINGDATA_{name}", raising=False)
    with pytest.raises(RuntimeError, match="AMAZINGDATA_USERNAME"):
        E2E.main()


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
