# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary users are individual ETF traders who monitor strategy signals and manage their own user-entered account snapshot. Account information belongs to the authenticated OIDC subject; signal data is shared across authorized users.

## Product Purpose

A web decision-support workspace for monitoring current strategy signals and reviewing user-reported account state, NAV, and imported trade files. Success means users can understand the current signal and account-data limitations, safely maintain their snapshot, and explicitly review and confirm supported trade imports.

## Positioning

A read-oriented decision-support desk built on live market data, strategy signals, and explicitly user-entered account data. It is not a broker account system, order-entry terminal, or trade execution service.

## Operating Context

Users may monitor live signals through an SSE stream and inspect account snapshot/NAV data. They may create or replace their account snapshot and preview then explicitly confirm supported Galaxy Securities trade CSV imports. OIDC identity is provided by a trusted external identity provider; the application validates access tokens and does not handle user passwords or issue tokens.

## Capabilities and Constraints

- The Python backend at the repository root, managed by uv, is the only business backend. The existing FastAPI REST APIs are authoritative; the Next.js frontend must not reimplement or alter their business logic.
- Signal API: authenticated current-signal query and SSE stream with event cursor recovery. Signals can be unavailable or stale; they are informational and do not represent orders or executed trades.
- Account API: per-authenticated-subject current snapshot, conditional full replacement using ETag preconditions, current NAV, and Galaxy CSV trade-import preview/confirmation. There is no account history query or delete route.
- Account snapshots and imported trades are user-provided and not broker-verified. Imported trades do not update the account snapshot.
- NAV is unavailable when the snapshot or required quote is missing, future-dated, stale, or otherwise unusable; the client must not present partial valuation as a complete NAV.
- The frontend lives in `frontend/`, uses Next.js App Router, TypeScript, Tailwind CSS, and Bun, and manages dependencies independently from Python/uv.
- First-version workflow scope: signal monitoring, account snapshot and NAV, and trade-file preview and confirmation.
- Product language is Chinese with Asia/Shanghai timestamps. The user-approved local integration runs the frontend, Python APIs, gateway, Keycloak, RabbitMQ and Redis together; browser API requests use same-origin `/api` rewrites to the existing gateway. Device authorization and page-memory access tokens remain in use: reload or token expiry requires reauthorization, without browser refresh-token persistence.

## Brand Commitments

The interface is a professional operational web application: prioritize functionality, information hierarchy, and efficient task completion; use a modern, restrained visual language; design desktop-first while supporting mobile. Avoid generic template-like AI dashboard styling. Do not fabricate backend capabilities or imply trading execution, broker verification, or guaranteed outcomes.

## Evidence on Hand

- Existing service contracts: `docs/signal-monitoring-api.md` and `docs/account-snapshot-api.md`.
- Domain terminology and truth constraints: root `CONTEXT.md`.
- User guide: `docs/user-guide.md`.
- Existing REST implementations: `src/a500_t_trader/signal_monitor.py` and `src/a500_t_trader/account_snapshot_api.py`.
- The frontend workspace now exposes the approved signals, conditional snapshot editing, truthful NAV unavailable states, and trade preview/confirmation flows. Account drafts survive ETag conflicts; same-subject reauthorization can restore persisted import batches. No order-entry, deployment management, or account-history workflow is provided.

## Product Principles

1. Preserve the Python REST API as the sole source of business behavior and data contracts.
2. Make data provenance, freshness, and unavailable states visible wherever they affect decisions.
3. Keep account updates and trade import confirmation explicit, conditional, and recoverable from errors.
4. Distinguish strategy signals and imported/user-entered records from broker-confirmed or executed activity.
5. Design for efficient keyboard-accessible operation across desktop and mobile layouts.

## Accessibility & Inclusion

Support keyboard operation and accessible focus, labels, and status/error feedback across forms and interactive flows. Maintain usable responsive layouts on mobile as well as desktop.
