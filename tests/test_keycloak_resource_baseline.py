from __future__ import annotations

import importlib.util
from pathlib import Path

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


def test_development_mode_or_unpinned_image_is_rejected(monkeypatch) -> None:
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


def test_resource_sampling_uses_the_selected_compose_definition(monkeypatch) -> None:
    import argparse
    import subprocess

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
    observed = []
    inputs = iter(["", "", "", "1,2"])
    monkeypatch.setattr("builtins.input", lambda: next(inputs))
    monkeypatch.setattr(
        baseline, "_run", lambda command, **kwargs: observed.append(command)
    )
    monkeypatch.setattr(baseline, "_ready", lambda *_args: None)
    monkeypatch.setattr(
        baseline,
        "_collect",
        lambda compose, services, seconds: (
            observed.append(compose) or [{"memory_bytes": 10, "cpu_percent_sum": 1}]
        ),
    )

    def collect_until_stopped(compose, services, stop, output):
        observed.append(compose)
        output.append({"memory_bytes": 20, "cpu_percent_sum": 2})
        stop.set()

    monkeypatch.setattr(baseline, "_collect_until_stopped", collect_until_stopped)
    monkeypatch.setattr(
        baseline.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    args = argparse.Namespace(
        sample_service=["keycloak", "postgres"],
        readiness_url="http://localhost/health",
    )

    baseline._run_round(definition, files, args, 1)

    expected = [*definition, "-f", files[0], "-f", files[1]]
    assert observed[1] == expected
    assert observed[2] == expected
