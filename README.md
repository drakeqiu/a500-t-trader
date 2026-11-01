# A500 T-Trader

A minimal Python project skeleton for A500 T-Trader. It currently contains only the
package entry point and development quality checks.

## Development

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
```

Run all quality checks and the package entry point:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run python -m a500_t_trader
```

The smoke test is run with `uv run pytest`.

## AmazingData market-data queries

`AmazingDataProvider` logs in using AmazingData's internet API mode and exposes
request/response snapshot and bar queries through `MarketDataService`:

Create a local `.env` file containing `AMAZINGDATA_USERNAME`,
`AMAZINGDATA_PASSWORD`, `AMAZINGDATA_HOST`, and `AMAZINGDATA_PORT`. It is read
without printing its contents; environment variables override values from the
file. Startup also verifies that `$HOME/mdga_file` is writable before
initializing the SDK.

```python
from datetime import date

from a500_t_trader import create_provider_from_env
from a500_t_trader.market_data import MarketDataService

provider = create_provider_from_env()
market_data = MarketDataService(provider)

snapshot = market_data.get_snapshot()
today_bars = market_data.get_intraday_bars(interval_minutes=1)
history = market_data.get_history_bars(
    "563360", date(2026, 11, 2), date(2026, 11, 4), interval_minutes=5
)
```

These methods issue on-demand queries; they are not subscriptions or push
streams. Empty source results return `None` for a missing snapshot or an empty
tuple for bars. An unavailable or unauthorized capability raises
`CapabilityUnavailableError`, rather than being reported as empty data. Returned
bars retain source-reported gaps and identify an unfinished current bar with
`is_completed=False`.

## Git commits

This repository uses a repository-local Conventional Commits template. Git loads
`.gitmessage` when you run `git commit`:

```text
type(scope): description
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, and `ci`. The
scope is optional; a body and footers are optional. Use `!` before the colon or
a `BREAKING CHANGE:` footer to mark a breaking change. Edit the template as
needed; lines beginning with `#` are guidance and are omitted from the commit
message.

To enable it in a fresh clone:

```bash
git config --local commit.template .gitmessage
```

The configuration is local to the clone and is not shared through the repository.

## CI

GitHub Actions runs lint, format checking, strict mypy, and pytest on pushes and pull
requests. CI installs the locked environment with `uv sync --locked`.

## Automatic PR merging

Add the `automerge` label to a pull request targeting the default branch to enable
GitHub auto-merge with squash. Removing the label cancels pending auto-merge.
Only pull requests from branches in this repository are managed; fork pull requests
are ignored.

Enable auto-merge in the repository settings, then configure the default branch's
protection rules or ruleset to require the desired status checks, at least one
approval, and an up-to-date branch. GitHub auto-merge waits for those rules before
merging.
