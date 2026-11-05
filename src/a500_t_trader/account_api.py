"""Uvicorn entry point for the authenticated account API."""

from __future__ import annotations

import uvicorn

from .account_snapshot_api import create_app

app = create_app()


def main() -> None:
    uvicorn.run("a500_t_trader.account_api:app", host="0.0.0.0", port=8000)
