# Make real-time V1 signals the product boundary

## Status

Accepted

## Context

The project had accumulated research-backtest and certification requirements that made a passing historical certification a prerequisite for actionable quantities. The user clarified that the project is a quantitative tool for producing signals from live market data, not a quantitative framework, and does not require strict backtesting. V1 strategy parameters must be configurable, preferably through YAML, while the values in the V1 strategy rules remain the defaults.

## Decision

Make push-driven real-time signals the primary product behavior. Evaluate configured ETFs using completed five-minute bars; expose structured signals through the continuously running CLI and retain the strategy API. Emit no duplicate result for the same strategy identity and market timestamp. Insufficient or stale market data produces a reasoned unavailable/WAIT state, with no direction or quantity. The tool does not connect to a broker or place orders.

Keep historical replay as an optional observational research capability. Backtest certification is not a prerequisite for real-time signals or quantity suggestions. A suggested quantity remains advisory and depends on valid account, security-rule, market, and other required execution inputs; it is not a broker-verified or executable order.

V1 rule-file values are defaults. An optional YAML configuration may override only defined V1 parameters and explicitly list covered ETFs. Missing parameters use the rule defaults; unknown names or invalid values reject configuration loading. Results identify the full effective parameter set with a stable fingerprint. Configuration is loaded at process start or explicit reload, not on each evaluation. The repository provides a default YAML path, overridable by CLI; if no YAML exists, rule defaults apply, but the CLI never infers an ETF universe and requires an explicit non-empty ETF scope.

## Consequences

Historical performance and certification remain useful research records, but do not grant or revoke live signal or quantity-suggestion eligibility. Existing execution inputs continue to fail closed. The repository implementation provides a push-driven CLI with an injected provider, completed-bar aggregation, explicit ETF scope, stable effective-parameter identity, and YAML load/reload behavior. It does not connect to a broker or submit orders.
