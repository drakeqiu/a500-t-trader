# Use V1.0 as the advisory intraday strategy and gate actionable quantities on certification

## Status

Accepted

## Context

The initial advisory workflow left concrete intraday rules to later definition and used a simpler threshold strategy. The user supplied V1.0 rules and confirmed that it replaces the old `buy_below`/`sell_above` strategy for intraday signals. The application has no broker execution or verified live-account capability.

## Decision

Implement V1.0 behind a reusable strategy API called by the CLI; keep all output advisory and do not track or submit broker orders. Separate technical signal strength from risk vetoes. Missing/stale required account, market, or per-symbol trading-rule inputs prevent actionable quantities while still permitting technical analysis where possible. Explicitly associated confirmed fills alone update strategy-trading state; do not infer matches or track pending orders.

Require a passing backtest certification before returning actionable quantity permission. Certification is tied to the exact rules/parameters, implementation version, data and cost assumptions. Any V1.0 acceptance criterion without a source-defined numeric threshold must be supplied explicitly; an incomplete profile cannot certify. This avoids silently inventing risk limits or treating the existing simplified legacy backtest as validation of V1.0.

Detailed formulas, limits, account-state semantics, output contract, and acceptance criteria are specified in [`docs/design/intraday-etf-t-strategy-v1.md`](../design/intraday-etf-t-strategy-v1.md).
