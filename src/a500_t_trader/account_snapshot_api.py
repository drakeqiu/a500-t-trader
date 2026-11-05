"""Authenticated account snapshot HTTP API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

import jwt
from fastapi import FastAPI, Header, HTTPException, Request
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
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:[Zz]|[+-]\d{2}:\d{2})$"
)
_DECIMAL = re.compile(r"^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"invalid JSON constant: {constant}")


class _SnapshotValidationError(ValueError):
    def __init__(self, path: str, code: str) -> None:
        super().__init__(code)
        self.path = path
        self.code = code


def _problem(status: int, title: str, path: str, code: str) -> JSONResponse:
    return JSONResponse(
        {
            "type": "about:blank",
            "title": title,
            "status": status,
            "errors": [{"path": path, "code": code}],
        },
        status_code=status,
        media_type="application/problem+json",
    )


def _etag(snapshot: dict[str, object]) -> str:
    serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return '"' + hashlib.sha256(serialized.encode("utf-8")).hexdigest() + '"'


def _validate_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _SnapshotValidationError("$", "invalid_type")
    unknown = set(value) - _SNAPSHOT_FIELDS
    if unknown:
        raise _SnapshotValidationError(min(unknown), "unknown_field")
    missing = _SNAPSHOT_FIELDS - set(value)
    if missing:
        raise _SnapshotValidationError(min(missing), "missing_field")
    for field, item in value.items():
        if item is None:
            raise _SnapshotValidationError(field, "invalid_type")

    as_of = value["as_of"]
    if not isinstance(as_of, str) or not _RFC3339.fullmatch(as_of):
        raise _SnapshotValidationError("as_of", "invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(as_of.replace("Z", "+00:00").replace("z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError
    except (ValueError, OverflowError) as exc:
        raise _SnapshotValidationError("as_of", "invalid_timestamp") from exc

    def decimal_string(name: str, amount: object) -> str:
        if not isinstance(amount, str):
            raise _SnapshotValidationError(name, "invalid_type")
        if not _DECIMAL.fullmatch(amount):
            raise _SnapshotValidationError(name, "invalid_decimal")
        try:
            parsed_amount = Decimal(amount)
            if not parsed_amount.is_finite() or parsed_amount < 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError) as exc:
            raise _SnapshotValidationError(name, "invalid_decimal") from exc
        return amount

    def quantities(name: str) -> dict[str, int]:
        raw = value[name]
        if not isinstance(raw, dict):
            raise _SnapshotValidationError(name, "invalid_type")
        normalized: dict[str, int] = {}
        for symbol, quantity in raw.items():
            if not isinstance(symbol, str):
                raise _SnapshotValidationError(name, "invalid_security")
            try:
                code = normalize_symbol(symbol)
            except (TypeError, ValueError, AttributeError) as exc:
                raise _SnapshotValidationError(
                    f"{name}.{symbol}", "invalid_security"
                ) from exc
            if code in normalized:
                raise _SnapshotValidationError(f"{name}.{symbol}", "symbol_collision")
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
                raise _SnapshotValidationError(f"{name}.{symbol}", "invalid_quantity")
            normalized[code] = quantity
        return normalized

    holdings = quantities("holdings")
    floor = quantities("protected_floor")
    sellable = quantities("sellable")
    cost_basis = value["cost_basis"]
    if not isinstance(cost_basis, dict):
        raise _SnapshotValidationError("cost_basis", "invalid_type")
    normalized_cost: dict[str, str] = {}
    for symbol, amount in cost_basis.items():
        if not isinstance(symbol, str):
            raise _SnapshotValidationError("cost_basis", "invalid_security")
        try:
            code = normalize_symbol(symbol)
        except (TypeError, ValueError, AttributeError) as exc:
            raise _SnapshotValidationError(
                f"cost_basis.{symbol}", "invalid_security"
            ) from exc
        if code in normalized_cost:
            raise _SnapshotValidationError(f"cost_basis.{symbol}", "symbol_collision")
        if code not in holdings:
            raise _SnapshotValidationError(
                f"cost_basis.{symbol}", "invalid_cost_basis_key"
            )
        normalized_cost[code] = decimal_string(f"cost_basis.{symbol}", amount)
    for name, mapping in (("protected_floor", floor), ("sellable", sellable)):
        if set(mapping) != set(holdings):
            missing_code = min(set(holdings) - set(mapping), default=None)
            extra_code = min(set(mapping) - set(holdings), default=None)
            symbol = missing_code if missing_code is not None else extra_code
            raise _SnapshotValidationError(f"{name}.{symbol}", "quantity_key_mismatch")
    for code, holding in holdings.items():
        if floor[code] > holding:
            raise _SnapshotValidationError(
                f"protected_floor.{code}", "quantity_inconsistent"
            )
        if sellable[code] > holding - floor[code]:
            raise _SnapshotValidationError(f"sellable.{code}", "quantity_inconsistent")
    return {
        "as_of": as_of,
        "holdings": holdings,
        "protected_floor": floor,
        "sellable": sellable,
        "cost_basis": normalized_cost,
        "available_cash": decimal_string("available_cash", value["available_cash"]),
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
    async def put_account_snapshot(
        request: Request,
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
        try:
            snapshot = json.loads(
                await request.body(), parse_constant=_reject_json_constant
            )
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            ValueError,
            RecursionError,
        ):
            return _problem(400, "Invalid JSON", "$", "invalid_json")
        try:
            validated = _validate_snapshot(snapshot)
        except _SnapshotValidationError as exc:
            return _problem(422, "Invalid account snapshot", exc.path, exc.code)
        created, saved, etag = store.put_snapshot(
            subject, validated, if_match=if_match, if_none_match=if_none_match
        )
        return JSONResponse(saved, status_code=201 if created else 200, headers={"ETag": etag})

    return app
