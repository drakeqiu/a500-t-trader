# Apply minimum commission independently to every simulated fill

## Status

Accepted

## Context

Both historical backtest paths currently calculate commission only as notional multiplied by a basis-point rate. The requested fee schedule is a 0.5 bps rate with a 0.1 CNY minimum for every executed trade. Intraday orders can be partially filled over several bars, so the fee scope must be explicit.

## Decision

For each simulated fill independently, calculate commission as `max(fill_notional * commission_bps / 10_000, minimum_commission)`. Apply it on both buys and sells. A partial fill is a separate fill and incurs its own minimum; do not aggregate fees across fills from one submitted order. A minimum of zero disables the floor and preserves proportional-only behavior. Include both fee parameters in persisted run assumptions/results, and include the applicable per-fill fee in buy affordability and cash reservation so simulated cash never relies on an under-reserved proportional-only estimate.

## Consequences

The fee model is deterministic from the fill notional and two explicit parameters, but repeated partial fills can incur more total commission than a single fill of the same order. This is intentional per the requested per-trade rule. Commission remains separate from the adverse execution-price offset used for slippage/impact.
