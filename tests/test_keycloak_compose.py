from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_keycloak_is_standalone_and_persistent() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    local_compose = yaml.safe_load((ROOT / "compose.keycloak.yaml").read_text())

    assert "keycloak" not in compose["services"]
    assert "keycloak-data" not in compose.get("volumes", {})
    keycloak = local_compose["services"]["keycloak"]
    assert keycloak["image"] == "quay.io/keycloak/keycloak:26.2"
    assert keycloak["command"] == ["start-dev", "--import-realm"]
    assert (
        keycloak["environment"]["KC_BOOTSTRAP_ADMIN_USERNAME"]
        == "${KEYCLOAK_ADMIN_USERNAME:?KEYCLOAK_ADMIN_USERNAME is required}"
    )
    assert (
        keycloak["environment"]["KC_BOOTSTRAP_ADMIN_PASSWORD"]
        == "${KEYCLOAK_ADMIN_PASSWORD:?KEYCLOAK_ADMIN_PASSWORD is required}"
    )
    assert keycloak["ports"] == [
        "127.0.0.1:${KEYCLOAK_HOST_PORT:-8081}:8080"
    ]
    assert keycloak["environment"]["KC_HOSTNAME"] == "${KEYCLOAK_HOSTNAME:-}"
    assert compose["services"]["gateway"]["ports"] == [
        "127.0.0.1:${A500_GATEWAY_HOST_PORT:-8080}:8080"
    ]
    assert (
        "./keycloak/realm-a500.json:/opt/keycloak/data/import/realm-a500.json:ro"
        in keycloak["volumes"]
    )
    assert "keycloak-data:/opt/keycloak/data" in keycloak["volumes"]
    assert local_compose["volumes"]["keycloak-data"] is None


def test_imported_realm_has_public_device_client_and_a500_api_audience() -> None:
    realm = json.loads((ROOT / "keycloak/realm-a500.json").read_text())

    assert realm["realm"] == "a500"
    assert realm["registrationAllowed"] is False
    clients = {client["clientId"]: client for client in realm["clients"]}
    client = clients["a500-device-client"]
    assert client["publicClient"] is True
    assert client["secret"] == ""
    assert client["standardFlowEnabled"] is False
    assert client["directAccessGrantsEnabled"] is False
    assert client["oauth2DeviceAuthorizationGrantEnabled"] is True
    assert "users" not in realm or realm["users"] == []
    audience_mapper = next(
        mapper
        for mapper in client["protocolMappers"]
        if mapper["name"] == "a500-api-audience"
    )
    assert audience_mapper["config"]["included.custom.audience"] == "a500-api"
    assert audience_mapper["config"]["access.token.claim"] == "true"
