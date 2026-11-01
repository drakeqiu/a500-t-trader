"""Smoke tests for the command-line workflow entry point."""

import subprocess
import sys


def test_module_entry_point_exposes_premarket_workflow() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "a500_t_trader", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "premarket" in result.stdout
    assert result.stderr == ""
