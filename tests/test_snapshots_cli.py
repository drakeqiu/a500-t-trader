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

    def snapshot(self, *scope, cash="1000", holding="563360=100", sellable="100", restricted="0", snapshot_id="s1"):
        return self.run_cli("snapshots.py", "capture", "--id", snapshot_id, "--at", "2026-10-31T10:00:00Z",
                            "--cash", cash, "--holding", holding, "--sellable-holding", f"563360={sellable}",
                            "--t1-restricted-holding", f"563360={restricted}", *scope)

    def test_known_scope_overlays_only_uncovered_fills_and_keeps_ledger_unchanged(self):
        self.assertEqual(self.fill("covered", "2026-10-31T09:30:00Z").returncode, 0)
        before = self.run_cli("ledger.py", "balance").stdout
        captured = self.snapshot("--fills-through", "2026-10-31T10:00:00Z", "--orders-scope-known",
                                 cash="980", holding="563360=102")
        self.assertEqual(captured.returncode, 0, captured.stderr)
        self.assertIn("reconciliation: MATCH", captured.stdout)
        self.assertIn("cash delta (broker - ledger): 0.00", captured.stdout)
        self.assertIn("563360 holdings delta (broker - ledger): 0", captured.stdout)
        self.assertEqual(self.run_cli("ledger.py", "balance").stdout, before)
        self.assertEqual(self.fill("later", "2026-10-31T10:30:00Z").returncode, 0)
        resources = self.run_cli("snapshots.py", "resources")
        self.assertEqual(resources.returncode, 0, resources.stderr)
        self.assertIn("available cash: 960.00", resources.stdout)
        self.assertIn("563360: 104", resources.stdout)
        self.assertIn("openings: ALLOWED", resources.stdout)
        self.assertIn("cash: 960.00", self.run_cli("ledger.py", "balance").stdout)

    def test_confirmed_post_snapshot_account_events_update_resource_projection_once(self):
        self.assertEqual(self.snapshot("--fills-through", "2026-10-31T10:00:00Z",
                                       "--orders-scope-known").returncode, 0)
        for args in (
                ("cash-flow", "--id", "deposit", "--at", "2026-10-31T10:30:00Z",
                 "--direction", "in", "--amount", "50", "--confirm-adjustment"),
                ("holding-adjustment", "--id", "transfer", "--at", "2026-10-31T10:31:00Z",
                 "--symbol", "563360", "--direction", "in", "--quantity", "3", "--confirm-adjustment")):
            result = self.run_cli("shadow.py", *args)
            self.assertEqual(result.returncode, 0, result.stderr)
        resources = self.run_cli("snapshots.py", "resources")
        self.assertEqual(resources.returncode, 0, resources.stderr)
        self.assertIn("projected total cash: 1050.00", resources.stdout)
        self.assertIn("563360: 103", resources.stdout)
        self.assertIn("cash: 1050.00", self.run_cli("ledger.py", "balance").stdout)

    def test_snapshot_mismatch_reports_deltas_and_requires_explicit_reconciliation(self):
        captured = self.snapshot("--fills-through", "2026-10-31T10:00:00Z",
                                 "--orders-scope-known", cash="999", holding="563360=98",
                                 sellable="98", restricted="0")
        self.assertEqual(captured.returncode, 0, captured.stderr)
        self.assertIn("cash delta (broker - ledger): -1.00", captured.stdout)
        self.assertIn("563360 holdings delta (broker - ledger): -2", captured.stdout)
        self.assertIn("explicit reconciliation confirmation required", captured.stdout)
    def test_post_snapshot_sell_proceeds_do_not_inflate_entry_cash(self):
        self.assertEqual(self.snapshot("--fills-through", "2026-10-31T10:00:00Z",
                                       "--orders-scope-known").returncode, 0)
        self.assertEqual(self.fill("sale", "2026-10-31T10:30:00Z", side="sell",
                                   quantity="2", price="10").returncode, 0)
        resources = self.run_cli("snapshots.py", "resources")
        self.assertEqual(resources.returncode, 0, resources.stderr)
        self.assertIn("projected total cash: 1020.00", resources.stdout)
        self.assertIn("available cash: 1000.00", resources.stdout)

    def test_unknown_scope_blocks_new_open_projection(self):
        order = self.run_cli("orders.py", "record", "--id", "unknown-order", "--symbol", "563360",
                             "--side", "buy", "--quantity", "10", "--price", "5")
        self.assertEqual(order.returncode, 0, order.stderr)
        result = self.snapshot()
        self.assertEqual(result.returncode, 0, result.stderr)
        status = self.run_cli("snapshots.py", "status")
        self.assertIn("reconciliation: BLOCKED", status.stdout)
        self.assertIn("fills coverage: UNKNOWN", status.stdout)
        self.assertIn("orders coverage: UNKNOWN", status.stdout)
        self.assertIn("openings: BLOCKED", status.stdout)
        resources = self.run_cli("snapshots.py", "resources")
        self.assertIn("available cash: 1000.00", resources.stdout)
        self.assertIn("openings: BLOCKED", resources.stdout)

    def test_mismatch_blocks_and_covered_order_is_not_subtracted_twice(self):
        order = self.run_cli("orders.py", "record", "--id", "buy-1", "--symbol", "563360",
                             "--side", "buy", "--quantity", "10", "--price", "5")
        self.assertEqual(order.returncode, 0, order.stderr)
        result = self.run_cli("snapshots.py", "capture", "--id", "s1", "--at", "2026-10-31T10:00:00Z",
                              "--cash", "999", "--holding", "563360=100", "--fills-through", "2026-10-31T10:00:00Z",
                              "--orders-scope-known", "--covered-order", "buy-1", "--frozen-cash", "50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reconciliation: MISMATCH", result.stdout)
        status = self.run_cli("snapshots.py", "status")
        self.assertIn("covered order IDs: buy-1", status.stdout)
        self.assertIn("broker frozen cash: 50.00", status.stdout)
        resources = self.run_cli("snapshots.py", "resources")
        self.assertIn("available cash: 949.00", resources.stdout)
        self.assertIn("openings: BLOCKED", resources.stdout)

        self.fill("f1", "2026-10-31T09:30:00Z")
        result = self.snapshot("--fills-through", "2026-10-31T10:00:00Z", "--orders-scope-known",
                               cash="980", holding="563360=102", snapshot_id="s2")
        self.assertIn("reconciliation: MATCH", result.stdout)
        order = self.run_cli("orders.py", "record", "--id", "sell-1", "--symbol", "563360",
                             "--side", "sell", "--quantity", "10", "--price", "5")
        self.assertEqual(order.returncode, 0, order.stderr)
        resources = self.run_cli("snapshots.py", "resources")
        self.assertIn("available cash: 930.00", resources.stdout)
        self.assertIn("active order reserved cash: 50.00", resources.stdout)
        self.assertIn("563360: 92", resources.stdout)

    def test_resources_report_broker_confirmed_sellable_and_t1_quantities(self):
        result = self.snapshot("--fills-through", "2026-10-31T10:00:00Z", "--orders-scope-known",
                               "--frozen-cash", "25", "--frozen-holding", "563360=10", sellable="80", restricted="20")
        self.assertEqual(result.returncode, 0, result.stderr)
        fill = self.fill("same-day-buy", "2026-10-31T10:30:00Z", quantity="5")
        self.assertEqual(fill.returncode, 0, fill.stderr)
        resources = self.run_cli("snapshots.py", "resources")
        self.assertEqual(resources.returncode, 0, resources.stderr)
        self.assertIn("available cash: 925.00", resources.stdout)
        self.assertIn("broker frozen cash: 25.00", resources.stdout)
        self.assertIn("563360: 95", resources.stdout)
        self.assertIn("563360 sellable quantity: 70; T+1 restricted quantity: 25", resources.stdout)
        self.assertIn("sellability evidence: broker-confirmed snapshot; T+1 quantities are not inferred from weekday calendars", resources.stdout)

    def test_same_day_sell_reduces_sellable_without_promoting_t1_buys(self):
        result = self.snapshot("--fills-through", "2026-10-31T10:00:00Z",
                               "--orders-scope-known", sellable="80", restricted="20")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fill("buy-after-snapshot", "2026-10-31T10:30:00Z",
                                   quantity="5").returncode, 0)
        self.assertEqual(self.fill("sell-after-snapshot", "2026-10-31T10:30:00Z",
                                   side="sell", quantity="5").returncode, 0)
        resources = self.run_cli("snapshots.py", "resources")
        self.assertEqual(resources.returncode, 0, resources.stderr)
        self.assertIn("563360 sellable quantity: 75; T+1 restricted quantity: 25", resources.stdout)

    def test_resource_overlay_ignores_superseded_fill_versions(self):
        result = self.snapshot("--fills-through", "2026-10-31T10:00:00Z",
                               "--orders-scope-known")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fill("late-buy", "2026-10-31T10:30:00Z",
                                   quantity="2").returncode, 0)
        correction = self.run_cli("ledger.py", "correct", "--id", "late-buy-v2",
                                  "--supersedes", "late-buy", "--at", "2026-10-31T10:30:00Z",
                                  "--symbol", "563360", "--side", "buy", "--quantity", "3", "--price", "10")
        self.assertEqual(correction.returncode, 0, correction.stderr)
        resources = self.run_cli("snapshots.py", "resources")
        self.assertEqual(resources.returncode, 0, resources.stderr)
        self.assertIn("563360: 103", resources.stdout)
        self.assertIn("563360 sellable quantity: 100; T+1 restricted quantity: 3", resources.stdout)


if __name__ == "__main__":
    unittest.main()

