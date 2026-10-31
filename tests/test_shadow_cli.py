import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "ledger.py"
SHADOW = ROOT / "shadow.py"


class ShadowCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "account.sqlite"
        self.ledger("anchor", "--effective-at", "2026-01-02T09:30:00Z", "--cash", "10000",
                    "--holding", "563360=100", "--included-through", "2026-01-02T09:29:59Z")

    def tearDown(self):
        self.temp.cleanup()

    def ledger(self, *args):
        return subprocess.run([sys.executable, str(LEDGER), "--db", str(self.db), *args],
                              text=True, capture_output=True)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SHADOW), "--db", str(self.db), *args],
                              text=True, capture_output=True)

    def test_confirmed_events_mirror_but_real_fills_change_excess(self):
        reset = self.cli("reset", "--confirm-reset", "--at", "2026-01-02T09:30:00Z")
        self.assertEqual(reset.returncode, 0, reset.stderr)
        flow = self.cli("cash-flow", "--id", "deposit-1", "--at", "2026-01-02T10:00:00Z",
                        "--direction", "in", "--amount", "100", "--confirm-adjustment")
        self.assertEqual(flow.returncode, 0, flow.stderr)
        holding = self.cli("holding-adjustment", "--id", "transfer-1", "--at", "2026-01-02T10:01:00Z",
                           "--symbol", "563360", "--direction", "in", "--quantity", "10", "--confirm-adjustment")
        self.assertEqual(holding.returncode, 0, holding.stderr)
        self.assertEqual(self.ledger("fill", "--id", "real-buy", "--at", "2026-01-02T10:02:00Z",
                                     "--symbol", "563360", "--side", "buy", "--quantity", "10", "--price", "10").returncode, 0)
        result = self.cli("compare", "--price", "563360=11")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("real equity: 11320.00", result.stdout)
        self.assertIn("shadow equity: 11310.00", result.stdout)
        self.assertIn("net excess: 10.00", result.stdout)
        balance = self.ledger("balance")
        self.assertEqual(balance.returncode, 0, balance.stderr)
        self.assertIn("deposit-1", balance.stdout)
        self.assertIn("transfer-1", balance.stdout)
        self.assertIn("cash: 10000.00", balance.stdout)
        self.assertIn("563360: 120", balance.stdout)
    def test_shadow_rejects_unsupported_security(self):
        self.cli("reset", "--confirm-reset", "--at", "2026-01-02T09:30:00Z")
        result = self.cli("holding-adjustment", "--id", "unsupported", "--at", "2026-01-02T10:00:00Z",
                          "--symbol", "510300", "--direction", "in", "--quantity", "1",
                          "--confirm-adjustment")
        self.assertEqual(result.returncode, 2)
        self.assertIn("security 563360", result.stderr)
    def test_impossible_mirror_is_atomic_and_requires_confirmed_reset(self):
        self.assertEqual(self.cli("reset", "--confirm-reset", "--at", "2026-01-02T09:30:00Z").returncode, 0)
        self.assertEqual(self.ledger("fill", "--id", "real-buy", "--at", "2026-01-02T10:00:00Z",
                                     "--symbol", "563360", "--side", "buy", "--quantity", "20", "--price", "10").returncode, 0)
        result = self.cli("holding-adjustment", "--id", "transfer-out", "--at", "2026-01-02T10:01:00Z",
                          "--symbol", "563360", "--direction", "out", "--quantity", "110", "--confirm-adjustment")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confirmed reset", result.stderr)
        duplicate = self.cli("holding-adjustment", "--id", "transfer-out", "--at", "2026-01-02T10:01:00Z",
                             "--symbol", "563360", "--direction", "out", "--quantity", "110", "--confirm-adjustment")
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertEqual(self.cli("reset").returncode, 2)
        self.assertEqual(self.cli("reset", "--confirm-reset").returncode, 0)
        self.assertIn("net excess: 0.00", self.cli("compare", "--price", "563360=10").stdout)

    def test_cash_out_rejects_negative_real_cash(self):
        self.assertEqual(self.cli("reset", "--confirm-reset", "--at", "2026-01-02T09:30:00Z").returncode, 0)
        result = self.cli("cash-flow", "--id", "withdrawal", "--at", "2026-01-02T10:00:00Z",
                          "--direction", "out", "--amount", "10001", "--confirm-adjustment")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("insufficient real cash", result.stderr)

    def test_account_adjustment_requires_explicit_confirmation(self):
        result = self.cli("cash-flow", "--id", "unconfirmed", "--at", "2026-01-02T10:00:00Z",
                          "--direction", "in", "--amount", "10")
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --confirm-adjustment", result.stderr)
        self.assertNotIn("unconfirmed", self.ledger("balance").stdout)

    def test_reset_requires_a_real_anchor(self):
        empty_db = Path(self.temp.name) / "unanchored.sqlite"
        result = subprocess.run(
            [sys.executable, str(SHADOW), "--db", str(empty_db), "reset", "--confirm-reset"],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("confirm a real account anchor first", result.stderr)



if __name__ == "__main__":
    unittest.main()
