from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _percentile95(values: list[float]) -> float:
    if not values:
        raise ValueError("at least one sample is required")
    ordered = sorted(values)
    return ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)]


def _bytes(value: str) -> int:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*", value)
    if not match:
        raise ValueError("unrecognized container memory value")
    amount, unit = match.groups()
    scale = {"B": 1, "kB": 1000, "KB": 1000, "MB": 1000**2, "GB": 1000**3,
             "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
    if unit not in scale:
        raise ValueError("unrecognized container memory unit")
    return int(float(amount) * scale[unit])


def _stats(raw: str) -> list[dict[str, float]]:
    rows = json.loads(raw)
    if isinstance(rows, dict):
        rows = [rows]
    result = []
    for row in rows:
        memory = str(row.get("MemUsage", row.get("MemUsageBytes", "")))
        if "/" in memory:
            memory = memory.split("/", 1)[0].strip()
        cpu = str(row.get("CPU", row.get("CPUPercentage", ""))).strip().removesuffix("%")
        result.append({"memory_bytes": float(_bytes(memory)), "cpu_percent": float(cpu)})
    if not result:
        raise ValueError("container stats returned no rows")
    return result


def _compose(args: argparse.Namespace) -> list[str]:
    command = shlex.split(args.compose_command)
    if not command:
        raise ValueError("compose command cannot be empty")
    return [*command, "--env-file", os.devnull, "--project-name", args.project]


def _run(command: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    # Compose output may contain environment values; never forward it to the report/terminal.
    return subprocess.run(command, text=True, capture_output=True, check=True, timeout=timeout)


def _ready(url: str, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(1)
    raise TimeoutError("identity readiness endpoint did not become available")


def _container_ids(compose: list[str], services: list[str]) -> list[str]:
    ids: set[str] = set()
    for service in services:
        result = _run([*compose, "ps", "-q", service], timeout=30)
        ids.update(result.stdout.split())
    if not ids:
        raise RuntimeError("no containers found for the selected services")
    return sorted(ids)


def _sample(compose: list[str], services: list[str], elapsed: float) -> dict[str, Any]:
    ids = _container_ids(compose, services)
    result = _run(["podman", "stats", "--no-stream", "--format", "json", *ids], timeout=30)
    rows = _stats(result.stdout)
    return {
        "elapsed_seconds": round(elapsed, 3),
        "container_count": len(rows),
        "memory_bytes": int(sum(row["memory_bytes"] for row in rows)),
        "cpu_percent_sum": round(sum(row["cpu_percent"] for row in rows), 4),
    }


def _collect(compose: list[str], services: list[str], seconds: int) -> list[dict[str, Any]]:
    samples = []
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        elapsed = time.monotonic() - started
        samples.append(_sample(compose, services, elapsed))
        time.sleep(max(0, 1 - (time.monotonic() - started - elapsed)))
    return samples


def _collect_until_stopped(compose: list[str], services: list[str], stop: Any, output: list[dict[str, Any]]) -> None:
    started = time.monotonic()
    while not stop.is_set():
        elapsed = time.monotonic() - started
        output.append(_sample(compose, services, elapsed))
        stop.wait(max(0, 1 - (time.monotonic() - started - elapsed)))


def _deployment_mode(compose: list[str], files: list[str], service: str) -> str:
    result = _run(
        [*compose, *sum((["-f", path] for path in files), []), "config", "--format", "json"],
        timeout=30,
    )
    config = json.loads(result.stdout)
    target = config.get("services", {}).get(service)
    if not isinstance(target, dict):
        raise ValueError("identity service is absent from the Compose configuration")
    command = json.dumps(target.get("command", "")).lower()
    image = str(target.get("image", ""))
    if "start-dev" in command:
        raise ValueError("development-mode Keycloak is not an acceptance baseline")
    if "@sha256:" not in image:
        raise ValueError("identity image must be pinned by sha256 digest")
    return image


def _run_round(compose: list[str], files: list[str], args: argparse.Namespace, number: int) -> dict[str, Any]:
    definition = [*compose, *sum((["-f", path] for path in files), [])]
    try:
        started = time.monotonic()
        _run([*definition, "up", "-d", "--no-build"], timeout=600)
        _ready(args.readiness_url)
        startup = time.monotonic() - started
        print(f"Round {number}: ready in {startup:.2f}s. Complete browser and device-flow warmup; press Enter when settled.", flush=True)
        input()
        print(f"Round {number}: collecting 600 seconds of idle samples.", flush=True)
        idle = _collect(definition, args.sample_service, 600)
        print("Press Enter to start the authorization-load sampler; immediately run browser and independent-device logins, with at most two concurrent.", flush=True)
        input()
        from threading import Event, Thread
        stop = Event()
        load: list[dict[str, Any]] = []
        sampler_errors: list[BaseException] = []

        def sample_load() -> None:
            try:
                _collect_until_stopped(definition, args.sample_service, stop, load)
            except BaseException as error:
                sampler_errors.append(error)
                stop.set()

        sampler = Thread(target=sample_load, daemon=True)
        sampler.start()
        print("Authorization load is being sampled. Run both logins, then press Enter.", flush=True)
        input()
        stop.set()
        sampler.join()
        if sampler_errors:
            raise RuntimeError("authorization-load sampler failed") from sampler_errors[0]
        print("Enter observed browser and device login durations in seconds, comma-separated (no credentials/tokens).", flush=True)
        login_times = [float(value.strip()) for value in input().split(",") if value.strip()]
        if not login_times or not load:
            raise ValueError("login durations and authorization-load samples are required")
        idle_memory = [sample["memory_bytes"] for sample in idle]
        return {
            "round": number,
            "startup_seconds": round(startup, 3),
            "idle": idle,
            "authorization_load": load,
            "login_seconds": login_times,
            "summary": {
                "idle_memory_p95_bytes": int(_percentile95(idle_memory)),
                "authorization_sampled_peak_memory_bytes": max(sample["memory_bytes"] for sample in load),
                "authorization_mean_cpu_percent_sum": round(statistics.mean(sample["cpu_percent_sum"] for sample in load), 4),
                "startup_seconds": round(startup, 3),
                "login_p95_seconds": round(_percentile95(login_times), 3),
            },
        }
    finally:
        cleanup = subprocess.run(
            [*definition, "down", "--volumes", "--remove-orphans"],
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if cleanup.returncode:
            raise RuntimeError("scoped Compose cleanup failed; inspect the unique baseline project")


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect three sanitized Keycloak resource rounds")
    parser.add_argument("--compose-file", action="append", required=True)
    parser.add_argument("--identity-service", required=True)
    parser.add_argument("--sample-service", action="append", required=True,
                        help="Identity service and each dedicated dependency; repeat for each service")
    parser.add_argument("--readiness-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compose-command", default=os.environ.get("PODMAN_COMPOSE", "podman-compose"))
    args = parser.parse_args()
    args.project = f"a500-kc-baseline-{os.urandom(6).hex()}"
    try:
        if args.output.exists():
            raise FileExistsError("output file already exists; choose a new evidence path")
        files = [str(Path(path).resolve()) for path in args.compose_file]
        if any(not Path(path).is_file() for path in files):
            raise FileNotFoundError("a Compose file does not exist")
        compose = _compose(args)
        image = _deployment_mode(compose, files, args.identity_service)
        print("Use identical host, deployment topology, user, clients, and hash parameters across comparison runs.", flush=True)
        topology = input("Describe included services and dedicated dependencies: ").strip()
        hash_parameters = input("Record password-hash algorithm and parameters (not secrets): ").strip()
        external_costs = input("List excluded external costs, e.g. SMTP: ").strip()
        rounds = [_run_round(compose, files, args, number) for number in range(1, 4)]
        report = {
            "method": "podman stats JSON sampled once per second; idle=600s after operator warmup; authorization sampling runs concurrently with browser and independent-device authorization; sampled peak is not a cgroup peak counter",
            "topology": topology,
            "identity_image": image,
            "included_services": args.sample_service,
            "password_hash_parameters": hash_parameters,
            "excluded_external_costs": external_costs,
            "rounds": rounds,
            "measurement_status": "observed; review raw sanitized per-round samples before comparison",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as evidence:
            json.dump(report, evidence, indent=2)
            evidence.write("\n")
        print(f"Sanitized evidence written to {args.output}")
        return 0
    except Exception as error:
        print(f"Baseline collection failed: {type(error).__name__}; resources in the unique project were cleanup-attempted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
