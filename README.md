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
