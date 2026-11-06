from __future__ import annotations

import importlib.util
import socket
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "compose-market-data-e2e.py"
SPEC = importlib.util.spec_from_file_location("compose_market_data_e2e", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
E2E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E2E)


def test_each_e2e_run_gets_distinct_compose_resources() -> None:
    projects = [E2E._project_name() for _ in range(2)]
    images = [E2E._consumer_image(project) for project in projects]

    assert len(set(projects)) == 2
    assert all(project.startswith("a500-market-data-e2e-") for project in projects)
    assert len(set(images)) == 2
    assert all(image.endswith("-consumer:local") for image in images)


def test_rabbitmq_port_is_nonzero_and_available_on_loopback() -> None:
    port = E2E._available_port()
    assert 0 < port <= 65535
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
