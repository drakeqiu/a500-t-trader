# Expose account snapshots as an authenticated REST resource

## Status

Accepted

## Context

The system currently accepts user-entered account snapshots through a local workflow and stores them as append-only records; no account REST route or user identity model exists. A remote API needs a stable current-state resource without implying broker verification, while whole-snapshot replacement risks overwriting newer input and history is valuable for audit and recovery.

## Decision

Expose `GET` and `PUT /v1/account-snapshot` as a single current snapshot per authenticated user. Authenticate with OIDC Bearer credentials and derive ownership from the verified principal; do not accept client-supplied user or account identifiers. `PUT` is a complete replacement and upsert: creation requires `If-None-Match: *`, replacement requires `If-Match`; return `201` for creation, `200` for replacement, `428` when the precondition is absent, and `412` when it is stale. `GET` returns `404` before the first snapshot and otherwise returns the snapshot, server `updated_at`, and `ETag`. Do not expose a delete or history-query route.

The snapshot includes `as_of`, `holdings`, `protected_floor`, `sellable`, `cost_basis`, and `available_cash`. `as_of` is an RFC 3339 timestamp with an offset and may be backdated; `updated_at` is server assigned. Encode monetary values as finite non-negative decimal strings. Require non-negative integer quantities, matching security keys among holdings, protected floor, and sellable mappings, and enforce `protected_floor <= holdings` and `sellable <= holdings - protected_floor`. `cost_basis` is always an object whose keys are a subset of holdings; omitted keys mean unknown cost. Reuse existing security-symbol normalization and reject collisions after normalization. Reject unknown fields, `null`, and invalid JSON types. Return `application/problem+json` with stable field paths/codes; use `400` for malformed JSON and `422` for invalid content.

Retain each successful complete snapshot with authenticated user, `as_of`, server write time, and the classification `user-entered; not broker-verified`; failed writes do not create successful history. Keep history internal for audit/recovery for the account lifecycle. Empty holdings express a cash-only or cleared position state; they do not delete the resource or its history.

## Consequences

The account model and persistence must represent the explicit protected floor and distinguish snapshot state time from server write time. The API requires authentication and authorization integration beyond the existing read-only signal service. Conditional writes make stale-client conflicts visible rather than silently choosing last-writer-wins. User-entered data remains advisory and must never be presented as broker-verified state.
