from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "keycloak-resource-baseline.py"
SPEC = importlib.util.spec_from_file_location("keycloak_resource_baseline", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BASELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASELINE)


def test_stats_extracts_memory_and_cpu_for_sampled_peak() -> None:
    rows = BASELINE._stats('[{"MemUsage":"1.5GiB / 4GiB","CPU":"12.5%"}]')
    assert rows == [{"memory_bytes": int(1.5 * 1024**3), "cpu_percent": 12.5}]


def test_percentile95_uses_nearest_rank() -> None:
    assert BASELINE._percentile95([1.0, 2.0, 3.0, 4.0, 5.0]) == 5.0


def test_development_mode_or_unpinned_image_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        BASELINE,
        "_run",
        lambda *_args, **_kwargs: type(
            "Result",
            (),
            {
                "stdout": (
                    '{"services":{"idp":{"image":"example/keycloak:latest",'
                    '"command":"start-dev"}}}'
                )
            },
        )(),
    )
    try:
        BASELINE._deployment_mode([], [], "idp")
    except ValueError as error:
        assert "development-mode" in str(error)
    else:
        raise AssertionError("development Keycloak must not qualify as a baseline")
