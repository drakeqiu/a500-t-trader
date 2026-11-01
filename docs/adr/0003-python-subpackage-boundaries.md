# Organize the application as one distribution with responsibility-based subpackages

## Status

Accepted

Implementation tracking: #43.

## Context

The application is currently distributed as one Python package, `a500_t_trader`, with CLI/workflow, market-data, strategy, backtest, and persistence modules under the same package. The project is maintained and exercised as one unit; its documented command-line entry point is `python -m a500_t_trader`.

The existing architecture already separates vendor APIs behind a normalized market-data boundary (ADR-0001) and keeps the ETF strategy advisory-only (ADR-0002). Organizing code into responsibility-based subpackages should make those boundaries easier to navigate without introducing separate distribution, versioning, or release workflows.

## Decision

Organize `a500_t_trader` as one distribution containing these internal subpackages:

| Subpackage    | Responsibility                                                                                        |
| ------------- | ----------------------------------------------------------------------------------------------------- |
| `market_data` | Normalized market-data contracts and service, provider implementations, and trading-calendar support. |
| `strategy`    | Pre-market planning, intraday monitoring, and post-market review workflows.                           |
| `backtest`    | Backtesting logic and its result/history persistence.                                                 |
| `cli`         | Command-line behavior and runtime/application assembly.                                               |

Keep account-ledger and workflow-specific persistence with their owning domain; do not introduce a generic storage package solely to centralize persistence. Preserve the existing domain interfaces and vendor-neutral market-data boundary while moving modules.

Update repository imports and tests to the new module locations. Do not retain compatibility aliases for old module paths. Keep `python -m a500_t_trader` as the documented command-line entry point.

This proposal changes internal organization only. It does not create independently installable distributions, alter strategy behavior, or change the CLI contract.

## Consequences

- Developers navigate the package by responsibility while dependency installation and releases remain unified.
- Moving modules requires updating in-repository import sites and tests together.
- Callers importing old internal module paths must update; no compatibility layer is planned.
- The top-level module execution command remains stable.
