import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "ledger.py"
CLI = ROOT / "groups.py"


class GroupsCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "account.sqlite"
        self.run_ledger("anchor", "--effective-at", "2026-01-02T09:00:00Z",
                        "--cash", "10000", "--holding", "563360=20",
                        "--included-through", "2026-01-02T08:59:59Z")

    def tearDown(self):
        self.temp.cleanup()

    def run_ledger(self, *args):
        return subprocess.run([sys.executable, str(LEDGER), "--db", str(self.db), *args],
                              text=True, capture_output=True)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), "--db", str(self.db), *args],
                              text=True, capture_output=True)

    def fill(self, fill_id, at, side, quantity, price):
        return self.run_ledger("fill", "--id", fill_id, "--at", at, "--symbol", "563360",
                               "--side", side, "--quantity", str(quantity), "--price", str(price))

    def test_positive_t_pairs_confirmed_fills_and_allocates_actual_fees_once(self):
        self.assertEqual(self.fill("sell-1", "2026-01-02T10:00:00Z", "sell", 10, "2.60").returncode, 0)
        self.assertEqual(self.fill("buy-1", "2026-01-02T10:10:00Z", "buy", 7, "2.50").returncode, 0)
        self.assertEqual(self.fill("buy-2", "2026-01-02T10:20:00Z", "buy", 5, "2.40").returncode, 0)
        for args in (("fee", "--fill-id", "sell-1", "--amount", "0.30"),
                     ("fee", "--fill-id", "buy-1", "--amount", "0.14"),
                     ("fee", "--fill-id", "buy-2", "--amount", "0.10"),
                     ("add", "--group", "morning", "--direction", "positive", "--fill-id", "sell-1"),
                     ("add", "--group", "morning", "--direction", "positive", "--fill-id", "buy-1"),
                     ("add", "--group", "morning", "--direction", "positive", "--fill-id", "buy-2")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
        report = self.cli("report", "--group", "morning")
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertIn("direction: positive", report.stdout)
        self.assertIn("paired quantity: 10", report.stdout)
        self.assertIn("paired net P&L: 0.80", report.stdout)
        self.assertIn("unpaired exposure: buy 2", report.stdout)
        self.assertIn("unpaired basis: 4.80", report.stdout)
        self.assertIn("unpaired fee: 0.04", report.stdout)
        self.assertEqual(self.cli("report", "--group", "morning").stdout, report.stdout)

    def test_reverse_t_pairs_confirmed_fills_and_reports_unpaired_side(self):
        self.assertEqual(self.fill("buy", "2026-01-02T10:00:00Z", "buy", 8, "2.00").returncode, 0)
        self.assertEqual(self.fill("sell", "2026-01-02T10:10:00Z", "sell", 5, "2.20").returncode, 0)
        for args in (("fee", "--fill-id", "buy", "--amount", "0.08"),
                     ("fee", "--fill-id", "sell", "--amount", "0.10"),
                     ("add", "--group", "reverse", "--direction", "reverse", "--fill-id", "buy"),
                     ("add", "--group", "reverse", "--direction", "reverse", "--fill-id", "sell")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
        report = self.cli("report", "--group", "reverse")
        self.assertIn("paired quantity: 5", report.stdout)
        self.assertIn("paired net P&L: 0.85", report.stdout)
        self.assertIn("unpaired exposure: buy 3", report.stdout)
        self.assertIn("unpaired fee: 0.03", report.stdout)

    def test_fees_require_existing_fills_and_conflicts_or_duplicate_group_membership_are_rejected(self):
        self.assertNotEqual(self.cli("fee", "--fill-id", "unknown", "--amount", "1").returncode, 0)
        self.assertEqual(self.fill("f", "2026-01-02T10:00:00Z", "sell", 1, "2.00").returncode, 0)
        fee = self.cli("fee", "--fill-id", "f", "--amount", "0.10")
        self.assertEqual(fee.returncode, 0, fee.stderr)
        self.assertEqual(self.cli("fee", "--fill-id", "f", "--amount", "0.10").returncode, 0)
        self.assertNotEqual(self.cli("fee", "--fill-id", "f", "--amount", "0.11").returncode, 0)
        add = ("add", "--group", "g", "--direction", "positive", "--fill-id", "f")
        self.assertEqual(self.cli(*add).returncode, 0)
        self.assertNotEqual(self.cli(*add).returncode, 0)
        self.assertNotEqual(self.cli("add", "--group", "other", "--direction", "positive",
                                     "--fill-id", "f").returncode, 0)


if __name__ == "__main__":
    unittest.main()
