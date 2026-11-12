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


def test_main_requires_real_provider_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("USERNAME", "PASSWORD", "HOST", "PORT"):
        monkeypatch.delenv(f"AD_{name}", raising=False)
    with pytest.raises(RuntimeError, match="AD_USERNAME"):
        E2E.main()


def test_https_rejects_plaintext_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(E2E, "_request", lambda _url: (400, "bad request"))
    E2E._check_no_plaintext(8443)
    monkeypatch.setattr(E2E, "_request", lambda _url: (401, "unauthorized"))
    with pytest.raises(AssertionError, match="明文"):
        E2E._check_no_plaintext(8443)


def test_https_requests_use_certificate_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = object()
    monkeypatch.setattr(E2E, "TLS_CONTEXT", context)

    def reject_untrusted(_request: Any, *, timeout: int, context: Any) -> None:
        assert context is E2E.TLS_CONTEXT
        raise E2E.URLError("certificate verify failed")

    monkeypatch.setattr(E2E, "urlopen", reject_untrusted)
    with pytest.raises(E2E.URLError, match="certificate verify failed"):
        E2E._request("https://localhost:8443/")
