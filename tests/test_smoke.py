"""Smoke tests for the package entry point."""

import subprocess
import sys


def test_module_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "a500_t_trader"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == "A500 T-Trader\n"
    assert result.stderr == ""
