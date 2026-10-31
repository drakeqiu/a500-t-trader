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

    def test_correction_and_late_fill_are_auditable_and_rebuild_requires_confirmation(self):
        self.assertEqual(self.cli("anchor", "--effective-at", "2026-01-02T09:00:00Z",
                                  "--cash", "1000", "--holding", "563360=100",
                                  "--included-through", "2026-01-02T08:59:59Z").returncode, 0)
        self.assertEqual(self.cli("fill", "--id", "later", "--at", "2026-01-02T11:00:00Z",
                                  "--recorded-at", "2026-01-02T11:01:00Z", "--symbol", "563360",
                                  "--side", "buy", "--quantity", "10", "--price", "2").returncode, 0)
        correction = self.cli("correct", "--id", "later-v2", "--supersedes", "later",
                              "--at", "2026-01-02T11:00:00Z", "--recorded-at", "2026-01-02T11:02:00Z",
                              "--symbol", "563360", "--side", "buy", "--quantity", "8", "--price", "2")
        self.assertEqual(correction.returncode, 0, correction.stderr)
        late = self.cli("fill", "--id", "late", "--at", "2026-01-02T10:00:00Z",
                        "--recorded-at", "2026-01-02T12:00:00Z", "--symbol", "563360",
                        "--side", "sell", "--quantity", "5", "--price", "3")
        self.assertEqual(late.returncode, 0, late.stderr)
        balance = self.cli("balance")
        self.assertIn("cash: 999.00", balance.stdout)
        self.assertIn("563360: 103", balance.stdout)
        self.assertIn("recorded at 2026-01-02T12:00:00+00:00", balance.stdout)
        self.assertIn("later-v2", balance.stdout)
        self.assertIn("supersedes later", balance.stdout)
        self.assertNotEqual(self.cli("rebuild").returncode, 0)
        rebuilt = self.cli("rebuild", "--confirm-rebuild")
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        self.assertIn("before cash: 999.00", rebuilt.stdout)
        self.assertIn("after cash: 999.00", rebuilt.stdout)
        not_confirmed = self.cli("reanchor", "--effective-at", "2026-01-02T12:00:00Z",
                                 "--cash", "999", "--holding", "563360=103",
                                 "--included-through", "2026-01-02T11:59:59Z")
        self.assertNotEqual(not_confirmed.returncode, 0)
        reanchored = self.cli("reanchor", "--confirm-reanchor", "--effective-at", "2026-01-02T12:00:00Z",
                              "--cash", "999", "--holding", "563360=103",
                              "--included-through", "2026-01-02T11:59:59Z")
        self.assertEqual(reanchored.returncode, 0, reanchored.stderr)
        self.assertIn("re-anchor confirmed", reanchored.stdout)
        self.assertIn("re-anchor 2026-01-02T09:00:00+00:00 ->", self.cli("balance").stdout)


    def test_invalid_correction_rolls_back_the_effective_version(self):
        self.assertEqual(self.cli("anchor", "--effective-at", "2026-01-02T09:00:00Z",
                                  "--cash", "1000", "--holding", "563360=10",
                                  "--included-through", "2026-01-02T08:59:59Z").returncode, 0)
        self.assertEqual(self.cli("fill", "--id", "sale", "--at", "2026-01-02T10:00:00Z",
                                  "--symbol", "563360", "--side", "sell", "--quantity", "5", "--price", "2").returncode, 0)
        before = self.cli("balance").stdout
        bad = self.cli("correct", "--id", "sale-v2", "--supersedes", "sale",
                       "--at", "2026-01-02T10:00:00Z", "--symbol", "563360", "--side", "sell",
                       "--quantity", "20", "--price", "2")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("insufficient holdings", bad.stderr)
        self.assertEqual(self.cli("balance").stdout, before)

if __name__ == "__main__":
    unittest.main()
