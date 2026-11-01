"""Preserve ``python -m a500_t_trader`` as the command-line entry point."""

from .cli.main import main as _main

if __name__ == "__main__":
    raise SystemExit(_main())
