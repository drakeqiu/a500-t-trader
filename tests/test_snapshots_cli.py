import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SnapshotsCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "account.sqlite"
        self.run_cli("ledger.py", "anchor", "--effective-at", "2026-10-31T09:00:00Z",
                     "--cash", "1000", "--holding", "563360=100",
                     "--included-through", "2026-10-31T08:59:59Z")

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, module, *args):
        return subprocess.run([sys.executable, str(ROOT / module), "--db", str(self.db), *args],
                              text=True, capture_output=True)

    def fill(self, identity, time, side="buy", quantity="2", price="10"):
        return self.run_cli("ledger.py", "fill", "--id", identity, "--at", time,
                            "--symbol", "563360", "--side", side, "--quantity", quantity,
                            "--price", price)

    def snapshot(self, *scope, cash="1000", holding="563360=100"):
        return self.run_cli("snapshots.py", "capture", "--id", "s1", "--at", "2026-10-31T10:00:00Z",
                            "--cash", cash, "--holding", holding, *scope)

    def test_known_scope_overlays_only_uncovered_fills_and_keeps_ledger_unchanged(self):
        self.assertEqual(self.fill("covered", "2026-10-31T09:30:00Z").returncode, 0)
        before = self.run_cli("ledger.py", "balance").stdout
        captured = self.snapshot("--fills-through", "2026-10-31T10:00:00Z", "--orders-scope-known",
                                 cash="980", holding="563360=102")
        self.assertEqual(captured.returncode, 0, captured.stderr)
        self.assertIn("reconciliation: MATCH", captured.stdout)
        self.assertEqual(self.run_cli("ledger.py", "balance").stdout, before)
        self.assertEqual(self.fill("later", "2026-10-31T10:30:00Z").returncode, 0)
        resources = self.run_cli("snapshots.py", "resources")
        self.assertEqual(resources.returncode, 0, resources.stderr)
        self.assertIn("cash: 960.00", resources.stdout)
        self.assertIn("563360: 104", resources.stdout)
        self.assertIn("openings: ALLOWED", resources.stdout)
        self.assertIn("cash: 960.00", self.run_cli("ledger.py", "balance").stdout)

    def test_unknown_scope_blocks_new_open_projection(self):
        result = self.snapshot()
        self.assertEqual(result.returncode, 0, result.stderr)
        status = self.run_cli("snapshots.py", "status")
        self.assertIn("reconciliation: BLOCKED", status.stdout)
        self.assertIn("fills coverage: UNKNOWN", status.stdout)
        self.assertIn("orders coverage: UNKNOWN", status.stdout)
        self.assertIn("openings: BLOCKED", status.stdout)

    def test_mismatch_blocks_and_covered_order_is_not_subtracted_twice(self):
        order = self.run_cli("orders.py", "record", "--id", "buy-1", "--symbol", "563360",
                             "--side", "buy", "--quantity", "10", "--price", "5")
        self.assertEqual(order.returncode, 0, order.stderr)
        result = self.run_cli("snapshots.py", "capture", "--id", "s1", "--at", "2026-10-31T10:00:00Z",
                              "--cash", "999", "--holding", "563360=100", "--fills-through", "2026-10-31T10:00:00Z",
                              "--orders-scope-known", "--covered-order", "buy-1", "--frozen-cash", "50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reconciliation: MISMATCH", result.stdout)
        resources = self.run_cli("snapshots.py", "resources")
        self.assertIn("cash: 949.00", resources.stdout)
        self.assertIn("openings: BLOCKED", resources.stdout)

    def test_uncovered_order_is_reserved_once_and_covered_fill_not_reapplied(self):
        self.fill("f1", "2026-10-31T09:30:00Z")
        self.run_cli("orders.py", "record", "--id", "sell-1", "--symbol", "563360",
                     "--side", "sell", "--quantity", "10", "--price", "5")
        result = self.snapshot("--fills-through", "2026-10-31T10:00:00Z", "--orders-scope-known",
                               cash="980", holding="563360=102")
        self.assertIn("reconciliation: MATCH", result.stdout)
        resources = self.run_cli("snapshots.py", "resources")
        self.assertIn("563360: 92", resources.stdout)
        self.assertIn("openings: ALLOWED", resources.stdout)


if __name__ == "__main__":
    unittest.main()
