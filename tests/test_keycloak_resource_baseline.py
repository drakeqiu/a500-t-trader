from __future__ import annotations

import argparse
import importlib.util
import subprocess
from pathlib import Path
from threading import Event

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


def test_resource_sampling_uses_the_selected_compose_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = BASELINE
    definition = [
        "podman",
        "compose",
        "--env-file",
        "/dev/null",
        "--project-name",
        "test",
    ]
    files = ["compose.yaml", "compose.production.yaml"]
    observed: list[list[str]] = []
    inputs = iter(["", "", "", "1,2"])
    monkeypatch.setattr("builtins.input", lambda: next(inputs))

    def fake_run(command: list[str], **kwargs: object) -> None:
        observed.append(command)

    monkeypatch.setattr(baseline, "_run", fake_run)
    monkeypatch.setattr(baseline, "_ready", lambda *_args: None)

    def collect(
        compose: list[str], services: list[str], seconds: int
    ) -> list[dict[str, int]]:
        observed.append(compose)
        return [{"memory_bytes": 10, "cpu_percent_sum": 1}]

    monkeypatch.setattr(baseline, "_collect", collect)

    def collect_until_stopped(
        compose: list[str],
        services: list[str],
        stop: Event,
        output: list[dict[str, int]],
    ) -> None:
        observed.append(compose)
        output.append({"memory_bytes": 20, "cpu_percent_sum": 2})
        stop.set()

    monkeypatch.setattr(baseline, "_collect_until_stopped", collect_until_stopped)

    def completed(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(baseline.subprocess, "run", completed)
    args = argparse.Namespace(
        sample_service=["keycloak", "postgres"],
        readiness_url="http://localhost/health",
    )

    baseline._run_round(definition, files, args, 1)

    expected = [*definition, "-f", files[0], "-f", files[1]]
    assert observed[1] == expected
    assert observed[2] == expected
