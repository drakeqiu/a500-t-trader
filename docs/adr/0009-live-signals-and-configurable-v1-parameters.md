# Make real-time V1 signals the product boundary

## Status

Accepted

## Context

The project had accumulated research-backtest and certification requirements that made a passing historical certification a prerequisite for actionable quantities. The user clarified that the project is a quantitative tool for producing signals from live market data, not a quantitative framework, and does not require strict backtesting. V1 strategy parameters must be configurable, preferably through YAML, while the values in the V1 strategy rules remain the defaults.

## Decision

Make push-driven real-time signals the primary product behavior. Evaluate configured ETFs using completed five-minute bars; expose structured signals through the continuously running CLI and retain the strategy API. Emit no duplicate result for the same strategy identity and market timestamp. Insufficient or stale market data produces a reasoned unavailable/WAIT state, with no direction or quantity. The tool does not connect to a broker or place orders.

Keep historical replay as an optional observational research capability. Backtest certification is not a prerequisite for real-time signals or quantity suggestions. A suggested quantity remains advisory and depends on valid account, security-rule, market, and other required execution inputs; it is not a broker-verified or executable order.

V1 rule-file values are defaults. An optional YAML configuration may override only defined V1 parameters and explicitly list covered ETFs. Unknown names or invalid values reject configuration loading. Results identify the full effective parameter set with a stable fingerprint. Configuration is loaded at process start or explicit reload, not on each evaluation. The repository provides a default YAML path, overridable by CLI; if no YAML exists, rule defaults apply, but the CLI never infers an ETF universe and requires an explicit non-empty ETF scope.

Live grid inputs are configured per ETF in the same YAML under `costs`: `tick_size`, `commission_rate`, `minimum_commission`, and `reference_quantity` are required for a covered ETF to produce an evaluated technical signal after sufficient history. The CLI calculates round-trip commission cost from the latest completed bar close and reference quantity, using `2 * max(price * quantity * commission_rate, minimum_commission) / (price * quantity)`. The reference quantity is a cost-estimation input, not an order quantity. Live cost modeling includes configured commissions only; it does not infer slippage or additional exchange fees. Missing cost inputs fail closed as unavailable rather than using implicit values. Cost inputs are included in the effective configuration fingerprint.

## Consequences

Historical performance and certification remain useful research records, but do not grant or revoke live signal or quantity-suggestion eligibility. Existing execution inputs continue to fail closed. The repository implementation provides a push-driven CLI with an injected provider, completed-bar aggregation, explicit ETF scope, stable effective-parameter identity, and YAML load/reload behavior. It does not connect to a broker or submit orders.
