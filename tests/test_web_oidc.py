from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from a500_t_trader.web_oidc import create_web_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(create_web_router())
    return TestClient(app)



def test_web_config_exposes_only_public_oidc_client_configuration(monkeypatch) -> None:
    monkeypatch.setenv("A500_OIDC_ISSUER", "https://identity.example.test/")
    monkeypatch.setenv("A500_OIDC_CLIENT_ID", "a500-web")
    monkeypatch.setenv("A500_OIDC_AUDIENCE", "a500-api")
    monkeypatch.setenv("A500_OIDC_CLIENT_SECRET", "never-return-this")

    with _client() as client:
        response = client.get("/web-config")

    assert response.status_code == 200
    assert response.json() == {
        "issuer": "https://identity.example.test",
        "clientId": "a500-web",
        "audience": "a500-api",
        "redirectUri": "http://testserver/",
    }
    assert "never-return-this" not in response.text


def test_web_config_fails_closed_when_oidc_client_is_not_configured(monkeypatch) -> None:
    for key in ("A500_OIDC_ISSUER", "A500_OIDC_CLIENT_ID", "A500_OIDC_AUDIENCE"):
        monkeypatch.delenv(key, raising=False)

    with _client() as client:
        response = client.get("/web-config")

    assert response.status_code == 503
