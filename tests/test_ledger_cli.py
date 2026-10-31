import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "ledger.py"


class LedgerCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "account.sqlite"

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), "--db", str(self.db), *args],
            text=True, capture_output=True,
        )

    def test_anchor_scope_idempotent_fill_and_traceable_persistent_balances(self):
        self.assertEqual(self.cli("anchor", "--effective-at", "2026-01-02T09:30:00Z",
                                  "--cash", "10000", "--holding", "563360=100",
                                  "--included-through", "2026-01-02T09:29:59Z").returncode, 0)
        # A fill inside the confirmed historical scope is recorded but not applied again.
        historical = self.cli("fill", "--id", "broker-1", "--at", "2026-01-02T09:00:00Z",
                              "--symbol", "563360", "--side", "buy", "--quantity", "20",
                              "--price", "2.5")
        self.assertEqual(historical.returncode, 0, historical.stderr)
        self.assertIn("included by anchor", historical.stdout)

        args = ("fill", "--id", "broker-2", "--at", "2026-01-02T10:00:00Z",
                "--symbol", "563360", "--side", "buy", "--quantity", "10", "--price", "2.5")
        self.assertEqual(self.cli(*args).returncode, 0)
        self.assertIn("duplicate", self.cli(*args).stdout)
        state = self.cli("balance")
        self.assertEqual(state.returncode, 0, state.stderr)
        self.assertIn("cash: 9975.00", state.stdout)
        self.assertIn("563360: 110", state.stdout)
        self.assertIn("anchor 2026-01-02T09:30:00+00:00", state.stdout)
        self.assertIn("broker-2", state.stdout)
        self.assertIn("buy 10 @ 2.5", state.stdout)
        self.assertEqual(self.cli("balance").stdout, state.stdout)

    def test_invalid_fill_does_not_partially_update_ledger(self):
        self.assertEqual(self.cli("anchor", "--effective-at", "2026-01-02T09:30:00Z",
                                  "--cash", "0", "--holding", "563360=0",
                                  "--included-through", "2026-01-02T09:29:59Z").returncode, 0)
        before = self.cli("balance").stdout
        bad = self.cli("fill", "--id", "bad-fill", "--at", "2026-01-02T10:00:00Z",
                       "--symbol", "563360", "--side", "buy", "--quantity", "100", "--price", "2")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("insufficient cash", bad.stderr)
        after = self.cli("balance")
        self.assertEqual(after.returncode, 0)
        self.assertEqual(after.stdout, before)
        self.assertNotIn("bad-fill", after.stdout)

        malformed = self.cli("fill", "--id", "malformed", "--at", "nonsense",
                             "--symbol", "563360", "--side", "sell", "--quantity", "1", "--price", "2")
        self.assertNotEqual(malformed.returncode, 0)
        self.assertEqual(self.cli("balance").stdout, before)


if __name__ == "__main__":
    unittest.main()
