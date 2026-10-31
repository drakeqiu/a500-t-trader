import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "orders.py"
LEDGER = ROOT / "ledger.py"


class OrdersCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "account.sqlite"
        self.run_ledger("anchor", "--effective-at", "2026-10-31T09:30:00Z",
                        "--cash", "1000", "--holding", "563360=100",
                        "--included-through", "2026-10-31T09:29:59Z")

    def tearDown(self):
        self.temp.cleanup()

    def run_ledger(self, *args):
        return subprocess.run([sys.executable, str(LEDGER), "--db", str(self.db), *args],
                              text=True, capture_output=True)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), "--db", str(self.db), *args],
                              text=True, capture_output=True)

    def test_active_orders_reserve_cash_and_holdings_until_terminal(self):
        fill = self.run_ledger("fill", "--id", "fill-1", "--at", "2026-10-31T10:00:00Z",
                               "--symbol", "563360", "--side", "buy", "--quantity", "2", "--price", "10")
        self.assertEqual(fill.returncode, 0, fill.stderr)
        self.assertEqual(self.cli("record", "--id", "buy-1", "--symbol", "563360",
                                  "--side", "buy", "--quantity", "20", "--price", "10").returncode, 0)
        self.assertEqual(self.cli("record", "--id", "sell-1", "--symbol", "563360",
                                  "--side", "sell", "--quantity", "30", "--price", "12").returncode, 0)

        active = self.cli("active")
        self.assertEqual(active.returncode, 0, active.stderr)
        self.assertIn("buy-1 open 563360 buy 20 @ 10.00; reserves cash 200.00", active.stdout)
        self.assertIn("sell-1 open 563360 sell 30 @ 12.00; reserves holdings 30", active.stdout)
        self.assertIn("reserved cash: 200.00", active.stdout)
        self.assertIn("reserved holdings:", active.stdout)
        self.assertIn("563360: 30", active.stdout)

        self.assertEqual(self.cli("status", "--id", "buy-1", "--status", "filled").returncode, 0)
        self.assertEqual(self.cli("cancel", "--id", "sell-1").returncode, 2)
        canceled = self.cli("cancel", "--id", "sell-1", "--confirmed")
        self.assertEqual(canceled.returncode, 0, canceled.stderr)
        state = self.cli("active")
        self.assertEqual(state.returncode, 0, state.stderr)
        self.assertNotIn("buy-1", state.stdout)
        self.assertNotIn("sell-1", state.stdout)
        balance = self.run_ledger("balance")
        self.assertIn("cash: 980.00", balance.stdout)
        self.assertIn("fill-1", balance.stdout)

    def test_partial_order_reserves_only_remaining_and_unavailable_resources_are_rejected(self):
        bad = self.cli("record", "--id", "too-much", "--symbol", "563360", "--side", "buy",
                       "--quantity", "101", "--price", "10")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("insufficient unreserved cash", bad.stderr)
        self.assertEqual(self.cli("record", "--id", "partial-1", "--symbol", "563360", "--side", "sell",
                                  "--quantity", "80", "--price", "12").returncode, 0)
        partial = self.cli("status", "--id", "partial-1", "--status", "partial",
                           "--remaining-quantity", "25")
        self.assertEqual(partial.returncode, 0, partial.stderr)
        active = self.cli("active")
        self.assertIn("partial-1 partial 563360 sell 25 @ 12.00; reserves holdings 25", active.stdout)
        done = self.cli("status", "--id", "partial-1", "--status", "filled",
                        "--remaining-quantity", "0")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("partial-1", self.cli("active").stdout)


if __name__ == "__main__":
    unittest.main()
