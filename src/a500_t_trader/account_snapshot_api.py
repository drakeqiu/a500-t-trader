"""Authenticated account snapshot HTTP API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Annotated, Any, Protocol, cast

import jwt
import psycopg
from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse
from jwt import PyJWKClient
from psycopg.rows import dict_row

from .account_ledger import (
    GALAXY_CLASSIFICATION,
    GALAXY_SOURCE,
    AccountLedger,
    _lock_imports,
    _same_trade,
    preview_galaxy_fills,
)
from .database import database_url, initialize_database
from .market_data import (
    CapabilityUnavailableError,
    MarketDataService,
    normalize_symbol,
)

CLASSIFICATION = "user-entered; not broker-verified"
SHARED_OIDC_AUDIENCE = "a500-api"


class Authenticator(Protocol):
    def __call__(self, token: str) -> str: ...


class OIDCAuthenticator:
    """Validate OIDC bearer JWT signatures and required identity claims."""

    def __init__(self, issuer: str, audience: str, jwks_uri: str) -> None:
        if audience != SHARED_OIDC_AUDIENCE:
            raise ValueError(
                f"OIDC audience must be the shared audience {SHARED_OIDC_AUDIENCE!r}"
            )
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.jwks = PyJWKClient(jwks_uri)

    @classmethod
    def from_environment(cls) -> OIDCAuthenticator:
        issuer = os.environ.get("OIDC_ISSUER")
        audience = os.environ.get("OIDC_AUDIENCE")
        jwks_uri = os.environ.get("OIDC_JWKS_URI")
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
    """PostgreSQL-backed per-subject current snapshot store."""

    def __init__(self, path: str) -> None:
        self.path = path
        initialize_database(self.path)

    def get_snapshot(self, subject: str) -> dict[str, object] | None:
        with psycopg.connect(self.path) as db:
            row = db.execute(
                "SELECT snapshot_json, updated_at FROM account_snapshot_current "
                "WHERE subject = %s",
                (subject,),
            ).fetchone()
        if row is None:
            return None
        snapshot = cast(dict[str, object], json.loads(row[0]))
        snapshot["updated_at"] = row[1]
        snapshot["classification"] = CLASSIFICATION
        return snapshot

    def history_for_subject(self, subject: str) -> list[dict[str, object]]:
        """Return retained audit records for persistence-boundary consumers."""
        with psycopg.connect(self.path, row_factory=dict_row) as db:
            rows = db.execute(
                "SELECT snapshot_json,updated_at,classification FROM "
                "account_snapshot_history WHERE subject=%s ORDER BY id",
                (subject,),
            ).fetchall()
        return [
            {
                "snapshot": json.loads(row["snapshot_json"]),
                "updated_at": row["updated_at"],
                "classification": row["classification"],
            }
            for row in rows
        ]

    def save_snapshot(
        self,
        subject: str,
        snapshot: Mapping[str, object],
        *,
        updated_at: str | None = None,
    ) -> None:
        """Persist a snapshot for service integration and test setup."""
        written_at = updated_at or datetime.now(UTC).isoformat()
        with psycopg.connect(self.path) as db:
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("account-snapshot:" + subject,),
            )
            db.execute(
                "INSERT INTO account_snapshot_current("
                "subject,snapshot_json,updated_at) "
                "VALUES(%s,%s,%s) ON CONFLICT(subject) DO UPDATE SET "
                "snapshot_json=excluded.snapshot_json, updated_at=excluded.updated_at",
                (subject, json.dumps(snapshot, separators=(",", ":")), written_at),
            )

    def put_snapshot(
        self,
        subject: str,
        snapshot: Mapping[str, object],
        *,
        if_match: str | None,
        if_none_match: str | None,
        updated_at: str | None = None,
    ) -> tuple[bool, dict[str, object], str]:
        """Conditionally replace the current snapshot and append its audit record."""
        written_at = updated_at or datetime.now(UTC).isoformat()
        with psycopg.connect(self.path) as db:
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("account-snapshot:" + subject,),
            )
            row = db.execute(
                "SELECT snapshot_json,updated_at "
                "FROM account_snapshot_current WHERE subject=%s",
                (subject,),
            ).fetchone()
            current = None
            if row is not None:
                current = cast(dict[str, object], json.loads(row[0]))
                current["updated_at"] = row[1]
                current["classification"] = CLASSIFICATION
            exists = row is not None
            if exists:
                assert current is not None
                if if_match is None:
                    raise HTTPException(status_code=428, detail="If-Match required")
                if if_match != _etag(current):
                    raise HTTPException(status_code=412, detail="ETag does not match")
            else:
                if if_match is not None:
                    raise HTTPException(
                        status_code=412, detail="Account snapshot does not exist"
                    )
                if if_none_match is None:
                    raise HTTPException(
                        status_code=428, detail="If-None-Match: * required"
                    )
                if if_none_match != "*":
                    raise HTTPException(
                        status_code=412, detail="If-None-Match must be *"
                    )
            db.execute(
                "INSERT INTO account_snapshot_current("
                "subject,snapshot_json,updated_at) "
                "VALUES(%s,%s,%s) ON CONFLICT(subject) DO UPDATE SET "
                "snapshot_json=excluded.snapshot_json,updated_at=excluded.updated_at",
                (subject, json.dumps(snapshot, separators=(",", ":")), written_at),
            )
            db.execute(
                "INSERT INTO account_snapshot_history("
                "subject,snapshot_json,as_of,updated_at,classification) "
                "VALUES(%s,%s,%s,%s,%s)",
                (
                    subject,
                    json.dumps(snapshot, separators=(",", ":")),
                    str(snapshot["as_of"]),
                    written_at,
                    CLASSIFICATION,
                ),
            )
            result = dict(snapshot)
            result["updated_at"] = written_at
            result["classification"] = CLASSIFICATION
            return not exists, result, _etag(result)


_SNAPSHOT_FIELDS = {
    "as_of",
    "holdings",
    "protected_floor",
    "sellable",
    "cost_basis",
    "available_cash",
}
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:(?P<second>\d{2})(?:\.\d+)?"
    r"(?:[Zz]|[+-]\d{2}:\d{2})$"
)
_LEAP_SECOND_DATES = frozenset(
    {
        "1972-06-30",
        "1972-12-31",
        "1973-12-31",
        "1974-12-31",
        "1975-12-31",
        "1976-12-31",
        "1977-12-31",
        "1978-12-31",
        "1979-12-31",
        "1981-06-30",
        "1982-06-30",
        "1983-06-30",
        "1985-06-30",
        "1987-12-31",
        "1989-12-31",
        "1990-12-31",
        "1992-06-30",
        "1993-06-30",
        "1994-06-30",
        "1995-12-31",
        "1997-06-30",
        "1998-12-31",
        "2005-12-31",
        "2008-12-31",
        "2012-06-30",
        "2015-06-30",
        "2016-12-31",
    }
)


def _valid_rfc3339_timestamp(value: str) -> bool:
    match = _RFC3339.fullmatch(value)
    if match is None:
        return False
    leap_second = match["second"] == "60"
    if leap_second:
        value = value.replace(":60", ":59", 1)
    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00").replace("z", "+00:00")
        )
        if parsed.utcoffset() is None:
            return False
        if not leap_second:
            return True
        utc_next_second = parsed.astimezone(UTC).replace(tzinfo=None)
        return (
            utc_next_second.hour == 23
            and utc_next_second.minute == 59
            and utc_next_second.second == 59
            and utc_next_second.date().isoformat() in _LEAP_SECOND_DATES
        )
    except ValueError, OverflowError:
        return False


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
    if not isinstance(as_of, str) or not _valid_rfc3339_timestamp(as_of):
        raise _SnapshotValidationError("as_of", "invalid_timestamp")

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
            if (
                not isinstance(quantity, int)
                or isinstance(quantity, bool)
                or quantity < 0
            ):
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


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, Decimal)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _authenticated_subject(auth: Authenticator, authorization: str | None) -> str:
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
        return auth(token)
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid bearer credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


MAX_TRADE_IMPORT_BYTES = 5 * 1024 * 1024
MAX_TRADE_IMPORT_ROWS = 10_000
TRADE_IMPORT_BATCH_TTL_SECONDS = 15 * 60


def create_app(
    database: str | None = None,
    authenticator: Authenticator | None = None,
    *,
    market_data: MarketDataService | None = None,
) -> FastAPI:
    """Create the account read API with replaceable auth and isolated storage."""
    path = database or database_url()
    auth = authenticator or OIDCAuthenticator.from_environment()
    store = AccountSnapshotStore(path)
    ledger = AccountLedger(path)
    app = FastAPI(
        title="A500 account snapshot API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.account_snapshot_store = store
    app.state.account_ledger = ledger
    app.state.market_data = market_data

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_schema(
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authenticated_subject(auth, authorization)
        return app.openapi()

    @app.get("/docs", include_in_schema=False)
    async def swagger_docs(
        authorization: str | None = Header(default=None),
    ) -> object:
        _authenticated_subject(auth, authorization)
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title=f"{app.title} - Swagger UI",
        )

    @app.get("/redoc", include_in_schema=False)
    async def redoc_docs(
        authorization: str | None = Header(default=None),
    ) -> object:
        _authenticated_subject(auth, authorization)
        return get_redoc_html(
            openapi_url="/openapi.json",
            title=f"{app.title} - ReDoc",
        )

    @app.post("/v1/trade-imports/previews")
    async def preview_trade_import(
        file: Annotated[UploadFile, File()],
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        subject = _authenticated_subject(auth, authorization)
        content = await file.read(MAX_TRADE_IMPORT_BYTES + 1)
        if len(content) > MAX_TRADE_IMPORT_BYTES:
            raise HTTPException(status_code=413, detail="Upload exceeds 5 MiB limit")
        result = preview_galaxy_fills(
            path, content, owner=subject, max_rows=MAX_TRADE_IMPORT_ROWS
        )
        error_details = cast(list[dict[str, object]], result["error_details"])
        if any(error["field"] == "limit" for error in error_details):
            raise HTTPException(status_code=413, detail="CSV exceeds 10,000 row limit")
        if any(error["field"] == "header" for error in error_details):
            raise HTTPException(status_code=422, detail="Unsupported Galaxy CSV format")
        records = result.pop("_records")
        batch_id = str(uuid.uuid4())
        now = time.time()
        with psycopg.connect(str(path)) as db:
            db.execute(
                """DELETE FROM trade_import_batches
                   WHERE expires_at<=%s AND confirmed_json IS NULL""",
                (now,),
            )
            db.execute(
                """INSERT INTO trade_import_batches(
                    batch_id,owner,records_json,errors_json,summary_json,
                    created_at,expires_at,confirmed_json
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,NULL)""",
                (
                    batch_id,
                    subject,
                    json.dumps(records, default=_json_default),
                    json.dumps(result["error_details"]),
                    json.dumps(
                        {
                            key: result[key]
                            for key in ("valid", "new", "duplicates", "errors")
                        }
                    ),
                    now,
                    now + TRADE_IMPORT_BATCH_TTL_SECONDS,
                ),
            )
        return JSONResponse(
            {
                "batch_id": batch_id,
                "valid": result["valid"],
                "new": result["new"],
                "duplicates": result["duplicates"],
                "errors": result["errors"],
                "error_details": result["error_details"],
            }
        )

    @app.get("/v1/trade-imports/previews/{batch_id}")
    def get_trade_import_result(
        batch_id: str,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        subject = _authenticated_subject(auth, authorization)
        with psycopg.connect(str(path), row_factory=dict_row) as db:
            batch = db.execute(
                """SELECT summary_json,errors_json,confirmed_json,expires_at
                   FROM trade_import_batches WHERE batch_id=%s AND owner=%s""",
                (batch_id, subject),
            ).fetchone()
        if batch is None or (
            batch["confirmed_json"] is None and batch["expires_at"] <= time.time()
        ):
            raise HTTPException(status_code=404, detail="Preview not found")
        summary = json.loads(batch["summary_json"])
        return JSONResponse(
            {
                "batch_id": batch_id,
                **summary,
                "error_details": json.loads(batch["errors_json"]),
                "confirmed": (
                    json.loads(batch["confirmed_json"])
                    if batch["confirmed_json"] is not None
                    else None
                ),
            }
        )

    @app.post("/v1/trade-imports/previews/{batch_id}/confirm")
    def confirm_trade_import(
        batch_id: str,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        subject = _authenticated_subject(auth, authorization)
        with psycopg.connect(str(path), row_factory=dict_row) as db:
            batch = db.execute(
                """SELECT records_json,errors_json,expires_at,confirmed_json
                   FROM trade_import_batches
                   WHERE batch_id=%s AND owner=%s FOR UPDATE""",
                (batch_id, subject),
            ).fetchone()
            if batch is None:
                raise HTTPException(status_code=404, detail="Preview not found")
            if batch["confirmed_json"] is not None:
                return JSONResponse(json.loads(batch["confirmed_json"]))
            if batch["expires_at"] <= time.time():
                raise HTTPException(status_code=404, detail="Preview not found")
            if json.loads(batch["errors_json"]):
                raise HTTPException(
                    status_code=409, detail="Preview contains validation errors"
                )
            result = _confirm_trade_import_records(
                db, json.loads(batch["records_json"]), subject
            )
            db.execute(
                """UPDATE trade_import_batches
                   SET confirmed_json=%s,records_json='[]' WHERE batch_id=%s""",
                (json.dumps(result), batch_id),
            )
        return JSONResponse(result)

    @app.get("/v1/account-nav")
    def get_account_nav(
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        subject = _authenticated_subject(auth, authorization)
        now = datetime.now(UTC)
        snapshot = store.get_snapshot(subject)
        if snapshot is None:
            return JSONResponse(
                {
                    "status": "unavailable",
                    "nav": None,
                    "reason": "account_snapshot_missing",
                },
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        as_of = datetime.fromisoformat(
            str(snapshot["as_of"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        unavailable: str | None = None
        if as_of > now:
            unavailable = "account_snapshot_future"
        elif now - as_of > timedelta(minutes=30):
            unavailable = "account_snapshot_stale"

        valued_holdings: list[dict[str, object]] = []
        cash = Decimal(str(snapshot["available_cash"]))
        total = cash
        market_values: list[Decimal] = []
        if unavailable is None:
            for symbol, raw_quantity in sorted(
                cast(dict[str, int], snapshot["holdings"]).items()
            ):
                quantity = int(raw_quantity)
                if quantity == 0:
                    continue
                if market_data is None:
                    unavailable = "market_data_unavailable"
                    break
                try:
                    quote = market_data.get_snapshot(symbol)
                except CapabilityUnavailableError:
                    unavailable = "market_data_unavailable"
                    break
                if quote is None or quote.last_price is None:
                    unavailable = "quote_missing"
                    break
                if not quote.last_price.is_finite() or quote.last_price <= 0:
                    unavailable = "quote_invalid_price"
                    break
                quote_as_of = quote.timestamp.astimezone(UTC)
                if quote_as_of > now:
                    unavailable = "quote_future"
                    break
                if now - quote_as_of > timedelta(minutes=30):
                    unavailable = "quote_stale"
                    break
                with localcontext() as context:
                    context.prec = (
                        len(quote.last_price.as_tuple().digits) + len(str(quantity)) + 1
                    )
                    market_value = Decimal(quantity) * quote.last_price
                market_values.append(market_value)
                valued_holdings.append(
                    {
                        "symbol": symbol,
                        "quantity": quantity,
                        "last_price": str(quote.last_price),
                        "market_value": str(market_value),
                        "as_of": quote.timestamp.isoformat(),
                    }
                )

        if unavailable is None:
            amounts = [cash, *market_values]
            min_exponent = min(
                cast(int, amount.as_tuple().exponent) for amount in amounts
            )
            max_adjusted = max(amount.adjusted() for amount in amounts)
            with localcontext() as context:
                context.prec = max_adjusted - min_exponent + len(str(len(amounts))) + 2
                total = sum(amounts, Decimal(0))

        if unavailable is not None:
            result = {
                "status": "unavailable",
                "currency": "CNY",
                "nav": None,
                "available_cash": snapshot["available_cash"],
                "account_snapshot_as_of": snapshot["as_of"],
                "calculated_at": now.isoformat(),
                "holdings": [],
                "classification": CLASSIFICATION,
                "reason": unavailable,
            }
        else:
            result = {
                "status": "available",
                "currency": "CNY",
                "nav": str(total),
                "available_cash": snapshot["available_cash"],
                "account_snapshot_as_of": snapshot["as_of"],
                "calculated_at": now.isoformat(),
                "holdings": valued_holdings,
                "classification": CLASSIFICATION,
            }
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/v1/account-snapshot")
    def get_account_snapshot(
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        subject = _authenticated_subject(auth, authorization)
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
        subject = _authenticated_subject(auth, authorization)
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
        return JSONResponse(
            saved, status_code=201 if created else 200, headers={"ETag": etag}
        )

    return app


def _confirm_trade_import_records(
    db: psycopg.Connection[dict[str, Any]], records: list[dict[str, object]], owner: str
) -> dict[str, object]:
    _lock_imports(db, owner)
    existing = {
        str(row["trade_number"]): dict(row)
        for row in db.execute(
            "SELECT * FROM imported_fills WHERE source=%s AND owner=%s",
            (GALAXY_SOURCE, owner),
        )
    }
    seen: dict[str, dict[str, object]] = {}
    candidates: list[dict[str, object]] = []
    duplicates = 0
    for record in records:
        number = str(record["trade_number"])
        previous = seen.get(number) or existing.get(number)
        if previous is not None:
            if not _same_trade(previous, record):
                raise HTTPException(
                    status_code=409, detail="Preview conflicts with existing trade"
                )
            duplicates += 1
            continue
        seen[number] = record
        candidates.append(record)
    ids: list[int] = []
    for record in candidates:
        cursor = db.execute(
            """INSERT INTO imported_fills(
                owner,timestamp,symbol,side,quantity,price,trade_amount,fees,
                stamp_duty,other_charges,occurrence_amount,source,trade_number,
                contract_number,classification
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (owner,)
            + tuple(
                record[key]
                for key in (
                    "timestamp",
                    "symbol",
                    "side",
                    "quantity",
                    "price",
                    "trade_amount",
                    "fees",
                    "stamp_duty",
                    "other_charges",
                    "occurrence_amount",
                    "source",
                    "trade_number",
                    "contract_number",
                )
            )
            + (GALAXY_CLASSIFICATION,),
        )
        row = cursor.fetchone()
        assert row is not None
        ids.append(int(row["id"]))
    return {
        "valid": len(records),
        "new": len(ids),
        "duplicates": duplicates,
        "errors": 0,
        "inserted_ids": ids,
        "confirmed": True,
    }
