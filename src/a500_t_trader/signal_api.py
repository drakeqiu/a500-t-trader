"""ASGI entry point for the async signal-monitoring API."""

from __future__ import annotations

import os

import uvicorn

from .signal_monitor import create_app


def main() -> None:
    uvicorn.run(
        "a500_t_trader.signal_api:app",
        host=os.environ.get("A500_SIGNAL_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("A500_SIGNAL_API_PORT", "8000")),
        reload=False,
    )


app = create_app()
