#!/usr/bin/env bash
# Local development only; Rauthy HTTP provides no transport confidentiality or integrity.
set -euo pipefail
cd "$(dirname "$0")/.."
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-changeme}"
export MARKET_DATA_MODE=replay
exec python3 scripts/start-local.py "$@"
