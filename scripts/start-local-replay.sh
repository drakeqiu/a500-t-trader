#!/usr/bin/env bash
# Local development only: Keycloak bootstrap credentials are intentionally public.
set -euo pipefail
cd "$(dirname "$0")/.."
export KEYCLOAK_ADMIN_USERNAME=admin
export KEYCLOAK_ADMIN_PASSWORD=changeme
export A500_MARKET_DATA_MODE=replay
exec python3 scripts/start-local.py "$@"
