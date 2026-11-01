# Keep strategy advisory-only and separate from execution

## Status

Accepted

The first strategy capability is an API that produces explainable, risk-gated suggestions; it does not connect to a broker or place orders. Account snapshots and actual trade records are entered by users and remain distinct from strategy suggestions and simulated fills, because this repository has no verified broker-account or execution capability. Persist account snapshots, strategy/grid state, signal history, and manual fills through an existing storage integration once its interface is confirmed; do not select or introduce a database before then.

Backtests and simulations use explicit future-bar fill assumptions, costs, and slippage, enforce T+1 and the minimum holding floor, and compare against a same-initial-conditions hold baseline. Risk controls can veto any strategy output; unavailable or stale required inputs prevent a tradable quantity recommendation.
