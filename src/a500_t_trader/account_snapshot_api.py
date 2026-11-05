"""Authenticated account snapshot HTTP API."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

import jwt
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from jwt import PyJWKClient

from .market_data import normalize_symbol

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
            db.execute(
                """CREATE TABLE IF NOT EXISTS account_snapshot_history (
                    id INTEGER PRIMARY KEY,
                    subject TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    classification TEXT NOT NULL
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
        """Persist a snapshot for service integration and test setup."""
        written_at = updated_at or datetime.now(UTC).isoformat()
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO account_snapshot_current(subject,snapshot_json,updated_at) "
                "VALUES(?,?,?) ON CONFLICT(subject) DO UPDATE SET "
                "snapshot_json=excluded.snapshot_json, updated_at=excluded.updated_at",
                (subject, json.dumps(snapshot, separators=(",", ":")), written_at),
            )

    def put_snapshot(
        self,
        subject: str,
        snapshot: dict[str, object],
        *,
        if_match: str | None,
        if_none_match: str | None,
        updated_at: str | None = None,
    ) -> tuple[bool, dict[str, object], str]:
        """Conditionally replace current state and append its audit record atomically."""
        written_at = updated_at or datetime.now(UTC).isoformat()
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT snapshot_json,updated_at FROM account_snapshot_current WHERE subject=?",
                (subject,),
            ).fetchone()
            current = None
            if row is not None:
                current = json.loads(row[0])
                current["updated_at"] = row[1]
                current["classification"] = CLASSIFICATION
            exists = row is not None
            if exists:
                if if_match is None:
                    raise HTTPException(status_code=428, detail="If-Match required")
                if if_match != _etag(current):
                    raise HTTPException(status_code=412, detail="ETag does not match")
            else:
                if if_match is not None:
                    raise HTTPException(status_code=412, detail="Account snapshot does not exist")
                if if_none_match is None:
                    raise HTTPException(status_code=428, detail="If-None-Match: * required")
                if if_none_match != "*":
                    raise HTTPException(status_code=412, detail="If-None-Match must be *")
            db.execute(
                "INSERT INTO account_snapshot_current(subject,snapshot_json,updated_at) "
                "VALUES(?,?,?) ON CONFLICT(subject) DO UPDATE SET "
                "snapshot_json=excluded.snapshot_json,updated_at=excluded.updated_at",
                (subject, json.dumps(snapshot, separators=(",", ":")), written_at),
            )
            db.execute(
                "INSERT INTO account_snapshot_history(subject,snapshot_json,as_of,updated_at,"
                "classification) VALUES(?,?,?,?,?)",
                (subject, json.dumps(snapshot, separators=(",", ":")), str(snapshot["as_of"]),
                 written_at, CLASSIFICATION),
            )
            result = dict(snapshot)
            result["updated_at"] = written_at
            result["classification"] = CLASSIFICATION
            return not exists, result, _etag(result)


_SNAPSHOT_FIELDS = {
    "as_of", "holdings", "protected_floor", "sellable", "cost_basis", "available_cash"
}


def _etag(snapshot: dict[str, object]) -> str:
    serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return '"' + hashlib.sha256(serialized.encode("utf-8")).hexdigest() + '"'


def _validate_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _SNAPSHOT_FIELDS:
        raise HTTPException(status_code=422, detail="Invalid snapshot fields")
    as_of = value["as_of"]
    if not isinstance(as_of, str):
        raise HTTPException(status_code=422, detail="as_of must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="as_of must include a timezone offset") from exc

    def decimal_string(name: str) -> str:
        raw = value[name]
        if not isinstance(raw, str):
            raise HTTPException(status_code=422, detail=f"{name} must be a decimal string")
        try:
            amount = Decimal(raw)
            if not amount.is_finite() or amount < 0:
                raise InvalidOperation
        except InvalidOperation as exc:
            raise HTTPException(status_code=422, detail=f"{name} must be finite and non-negative") from exc
        return raw

    def quantities(name: str) -> dict[str, int]:
        raw = value[name]
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail=f"{name} must be an object")
        normalized: dict[str, int] = {}
        for symbol, quantity in raw.items():
            try:
                code = normalize_symbol(symbol)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=f"Invalid security in {name}") from exc
            if code in normalized or not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
                raise HTTPException(status_code=422, detail=f"Invalid quantity in {name}")
            normalized[code] = quantity
        return normalized

    holdings = quantities("holdings")
    floor = quantities("protected_floor")
    sellable = quantities("sellable")
    cost_basis = value["cost_basis"]
    if not isinstance(cost_basis, dict):
        raise HTTPException(status_code=422, detail="cost_basis must be an object")
    normalized_cost: dict[str, str] = {}
    for symbol, amount in cost_basis.items():
        try:
            code = normalize_symbol(symbol)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid security in cost_basis") from exc
        if code in normalized_cost or code not in holdings:
            raise HTTPException(status_code=422, detail="Invalid cost_basis keys")
        normalized_cost[code] = amount
    for code in set(floor) | set(sellable):
        if code not in holdings:
            raise HTTPException(status_code=422, detail="Security keys must match holdings")
        if floor.get(code, 0) > holdings[code] or sellable.get(code, 0) > holdings[code] - floor.get(code, 0):
            raise HTTPException(status_code=422, detail="Invalid protected_floor or sellable quantity")
    for code in holdings:
        if floor.get(code, 0) + sellable.get(code, 0) > holdings[code]:
            raise HTTPException(status_code=422, detail="Invalid protected_floor or sellable quantity")
    for amount in normalized_cost.values():
        if not isinstance(amount, str):
            raise HTTPException(status_code=422, detail="cost_basis values must be decimal strings")
        try:
            parsed_amount = Decimal(amount)
            if not parsed_amount.is_finite() or parsed_amount < 0:
                raise InvalidOperation
        except InvalidOperation as exc:
            raise HTTPException(status_code=422, detail="Invalid cost_basis amount") from exc
    return {
        "as_of": as_of,
        "holdings": holdings,
        "protected_floor": floor,
        "sellable": sellable,
        "cost_basis": normalized_cost,
        "available_cash": decimal_string("available_cash"),
    }



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
        if authorization is None:
            raise HTTPException(
                status_code=401,
                detail="Bearer credentials required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        scheme, separator, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not separator or not token:
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
        return JSONResponse(snapshot, headers={"ETag": _etag(snapshot)})

    @app.put("/v1/account-snapshot")
    def put_account_snapshot(
        snapshot: object,
        authorization: str | None = Header(default=None),
        if_match: str | None = Header(default=None, alias="If-Match"),
        if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    ) -> JSONResponse:
        if authorization is None:
            raise HTTPException(status_code=401, detail="Bearer credentials required",
                                headers={"WWW-Authenticate": "Bearer"})
        scheme, separator, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not separator or not token:
            raise HTTPException(status_code=401, detail="Bearer credentials required",
                                headers={"WWW-Authenticate": "Bearer"})
        try:
            subject = auth(token)
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Invalid bearer credentials",
                                headers={"WWW-Authenticate": "Bearer"}) from exc
        validated = _validate_snapshot(snapshot)
        created, saved, etag = store.put_snapshot(
            subject, validated, if_match=if_match, if_none_match=if_none_match
        )
        return JSONResponse(saved, status_code=201 if created else 200, headers={"ETag": etag})

    return app
