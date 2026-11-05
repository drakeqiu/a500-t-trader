# Add authenticated user-scoped trade-file import API

## Status

Accepted

## Context

The Galaxy CSV importer currently targets a caller-selected SQLite ledger and assumes one Galaxy account per ledger; the authenticated account API already derives ownership from a verified OIDC `sub`, but imported-fill storage has no user ownership. A REST upload therefore needs explicit tenant isolation and must preserve the existing preview-before-write and unverified-trade boundaries.

## Decision

Add the trade-file import REST API to the existing authenticated account API. Accept multipart uploads of the existing Galaxy CSV format only; derive the owner exclusively from the verified OIDC `sub`. Store imported fills in the shared database with user-scoped ownership and include that owner in deduplication scope. Require a preview followed by explicit confirmation using a batch ID; parsed preview records expire after 15 minutes, raw files are not retained, and confirmation is idempotent with a queryable result. Process requests synchronously, with limits of 5 MiB and 10,000 records. Import records remain user-provided and unverified, are stored separately from manual and simulated fills, and never update cash, holdings, sellable quantity, protected floor, or account snapshots.

## Consequences

Imported-fill persistence and deduplication require a schema migration to add user ownership; existing rows need an explicit migration policy before the API can safely expose them to authenticated users. Preview-batch storage must retain parsed records and confirmation results only for the defined lifecycle, and the API must ensure expired batches cannot be confirmed. A ledger continues to represent one Galaxy account; supporting multiple broker accounts per user requires a separate account-identity and deduplication design.
