"""Authenticated HTTP reads for user-entered account snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import jwt
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from jwt import PyJWKClient

CLASSIFICATION = "user-entered; not broker-verified"


class Authenticator(Protocol):
    def __call__(self, token: str) -> str: ...


class OIDCAuthenticator:
    """Validate OIDC bearer JWT signatures and required identity claims."""

    def __init__(self, issuer: str, audience: str, jwks_uri: str) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.jwks = PyJWKClient(jwks_uri)

    @classmethod
    def from_environment(cls) -> OIDCAuthenticator:
        issuer = os.environ.get("A500_OIDC_ISSUER")
        audience = os.environ.get("A500_OIDC_AUDIENCE")
        jwks_uri = os.environ.get("A500_OIDC_JWKS_URI")
        if not issuer or not audience or not jwks_uri:
            raise RuntimeError("OIDC issuer, audience, and JWKS URI must be configured")
        return cls(issuer, audience, jwks_uri)

    def __call__(self, token: str) -> str:
        signing_key = self.jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256", "ES256"],
            audience=self.audience,
            issuer=self.issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise jwt.InvalidTokenError("subject must be a non-empty string")
        return subject


class AccountSnapshotStore:
    """SQLite-backed per-subject current snapshot store."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS account_snapshot_current (
                    subject TEXT PRIMARY KEY,
                    snapshot_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )

    def get_snapshot(self, subject: str) -> dict[str, object] | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT snapshot_json, updated_at FROM account_snapshot_current "
                "WHERE subject = ?",
                (subject,),
            ).fetchone()
        if row is None:
            return None
        snapshot = json.loads(row[0])
        snapshot["updated_at"] = row[1]
        snapshot["classification"] = CLASSIFICATION
        return snapshot

    def save_snapshot(
        self, subject: str, snapshot: dict[str, object], *, updated_at: str | None = None
    ) -> None:
        """Persist a complete snapshot for service integration and test setup."""
        written_at = updated_at or datetime.now(UTC).isoformat()
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO account_snapshot_current(subject,snapshot_json,updated_at) "
                "VALUES(?,?,?) ON CONFLICT(subject) DO UPDATE SET "
                "snapshot_json=excluded.snapshot_json, updated_at=excluded.updated_at",
                (subject, json.dumps(snapshot, separators=(",", ":")), written_at),
            )


def create_app(
    database: str | Path | None = None,
    authenticator: Authenticator | None = None,
) -> FastAPI:
    """Create the account read API with replaceable auth and isolated storage."""
    path = database or os.environ.get("A500_ACCOUNT_DATABASE", "accounts.sqlite3")
    auth = authenticator or OIDCAuthenticator.from_environment()
    store = AccountSnapshotStore(path)
    app = FastAPI(title="A500 account snapshot API")
    app.state.account_snapshot_store = store

    @app.get("/v1/account-snapshot")
    def get_account_snapshot(
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail="Bearer credentials required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            raise HTTPException(
                status_code=401,
                detail="Bearer credentials required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            subject = auth(token)
        except Exception as exc:
            raise HTTPException(
                status_code=401,
                detail="Invalid bearer credentials",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        snapshot = store.get_snapshot(subject)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Account snapshot not found")
        serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        etag = '"' + hashlib.sha256(serialized.encode("utf-8")).hexdigest() + '"'
        return JSONResponse(snapshot, headers={"ETag": etag})

    return app
