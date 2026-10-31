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

## CI

GitHub Actions runs lint, format checking, strict mypy, and pytest on pushes and pull
requests. CI installs the locked environment with `uv sync --locked`.
