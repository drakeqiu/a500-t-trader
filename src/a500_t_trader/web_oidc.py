"""Serve the A500 browser client from the deployed signal API origin."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

_WEB_ROOT = Path(__file__).with_name("web")


def create_web_router() -> APIRouter:
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_WEB_ROOT / "index.html", media_type="text/html")

    @router.get("/web/app.js", include_in_schema=False)
    async def app_js() -> FileResponse:
        return FileResponse(
            _WEB_ROOT / "app.js",
            media_type="text/javascript; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @router.get("/web/app.css", include_in_schema=False)
    async def app_css() -> FileResponse:
        return FileResponse(
            _WEB_ROOT / "app.css",
            media_type="text/css; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @router.get("/web-config", include_in_schema=False)
    async def web_config(request: Request) -> dict[str, str]:
        issuer = os.environ.get("A500_OIDC_ISSUER")
        client_id = os.environ.get("A500_OIDC_CLIENT_ID")
        audience = os.environ.get("A500_OIDC_AUDIENCE")
        if not issuer or not client_id or not audience:
            raise HTTPException(status_code=503, detail="OIDC web client is not configured")
        return {
            "issuer": issuer.rstrip("/"),
            "clientId": client_id,
            "audience": audience,
            "redirectUri": str(request.base_url),
        }

    return router
