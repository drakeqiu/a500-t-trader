# Add an asynchronous signal-monitoring API

## Status

Accepted

## Context

The project produces advisory real-time V1 signals from completed five-minute bars. A browser monitor needs HTTP access to current state and a continuous push channel, while the existing market-data consumer is synchronous and RabbitMQ delivers at least once. The API must not create a second strategy implementation or couple web-worker count to market-event consumption.

## Decision

Run signal calculation as a separate consumer of normalized RabbitMQ market events. It owns V1 evaluation and writes each symbol's latest signal state and ordered signal events to Redis. FastAPI is an asynchronous read/push service; it does not connect to tgw, consume RabbitMQ, calculate strategy signals, connect to a broker, or place orders. API instances share Redis state and event history. The design supports multiple API instances; signal computation remains a separate consumer role.

Expose `GET /healthz`, `GET /v1/signals`, and `GET /v1/signals/stream`. The HTTP query returns state for all configured ETFs when `symbols` is omitted; when supplied, every symbol must be configured or the request fails with a 4xx response. SSE requires an explicit `symbols` list and applies it only as a delivery filter; it does not change the globally configured calculation scope. Responses identify the strategy and effective parameter fingerprint and report the latest evaluation's event time, candidate/status, reasons, and applicable execution eligibility/quantity. Stale or unavailable state is not presented as a current actionable direction.

The SSE stream sends an initial per-symbol snapshot. Reconnects with a valid `Last-Event-ID` resume after that cursor without a new snapshot. If the cursor is invalid or older than the 24-hour retention window, send a reset event followed by a current snapshot. Redis Streams hold ordered events for the retention window; Redis also stores each symbol's latest state so API or calculation-process restarts can recover it, subject to the normal market-data freshness check. BUY, SELL, BUYBACK, and TAKE_PROFIT signal events are retained individually. WAIT, unavailable, and market/connection status updates may be coalesced to the latest state per symbol. Stream IDs are SSE event IDs; RabbitMQ duplicates must be tolerated and must not create duplicate evaluations for the same strategy identity and market timestamp.

Use FastAPI asynchronous request/stream handling and asynchronous Redis access. The existing synchronous RabbitMQ consumer remains outside the web process. Deploy behind a reverse proxy that restricts access and can add authentication later; the first version has no application-level authentication and must not be exposed directly to the public internet.

## Consequences

Redis Streams are required for reconnect replay; Redis Pub/Sub alone is insufficient. SSE is unidirectional, and the browser selects symbols at connection time; changing the selection requires opening a new stream. State/status updates are latest-value data, while directional signal events are retained individually. A cursor beyond retention requires client reset from a snapshot. Redis availability becomes necessary for signal-state queries and streaming. Authentication, authorization policy, and public exposure remain out of scope for the first version.
