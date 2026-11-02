# Give the RSI revision a distinct ruleset identity and cut over configuration

## Status

Accepted

## Context

The V1.0 strategy source was revised to add RSI scoring and RSI momentum filters while retaining the strategy's existing total score and trade thresholds. The source explicitly states that these rules have not been proven by historical backtesting and that the earlier implementation's results do not certify the revised rules. The source document is the rule authority and must remain unchanged during implementation work that aligns the project to it.

Existing project configuration and replay artifacts may describe the earlier KDJ-only implementation. Treating an old configuration or certification as if it described the RSI rules would make rule identity and research evidence ambiguous.

## Decision

- Give the RSI rules a distinct strategy/ruleset identity and a new major configuration schema. Bind the ruleset to the exact strategy-source SHA-256, parameters, and implementation version; a display name alone is not identity.
- Reject old configuration formats. Do not silently migrate, alias, or provide an automatic conversion tool.
- Do not carry forward old certification to the RSI rules. Each research group and any eventual certification must identify its exact rules, data, costs, execution assumptions, and implementation.
- Preserve the existing fixed-quantity technical-candidate replay as historical research material, but exclude it from RSI-rule comparisons and certification.
- Plan a controlled comparison using the same data, initial account state, cost model, and fill assumptions for: KDJ15/RSI0 baseline; KDJ8/RSI7 scoring split only; and the scoring split plus RSI momentum filters. Report incremental effects; fewer trades alone do not demonstrate improvement.
- Do not claim the comparison or certification is complete until the missing market history, account/execution inputs, and numeric certification gates are supplied and audited.

## Consequences

Old configuration consumers must create a configuration in the new schema. Old certification evidence remains attached only to its original ruleset. Historical candidate replay files remain available for traceability but are not evidence for the revised rules. The RSI comparison is deferred while documented data and execution prerequisites remain unresolved.

## References

- [Intraday ETF T strategy design](../design/intraday-etf-t-strategy-v1.md)
- [563360 backtest readiness record](../backtest-563360-v1.md)
- [V1.0 strategy source](../strategy/intraday-etf-t-v1/intraday_etf_t_quantitative_strategy_v1.0.md)
