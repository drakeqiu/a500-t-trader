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

    def run_module(self, module, *args):
        return subprocess.run([sys.executable, str(ROOT / module), "--db", str(self.db), *args],
                              text=True, capture_output=True)

    def test_only_supported_security_can_be_ordered(self):
        unsupported = self.cli("record", "--id", "other", "--symbol", "510300", "--side", "buy",
                              "--quantity", "100", "--price", "1")
        self.assertEqual(unsupported.returncode, 2)
        self.assertIn("security 563360", unsupported.stderr)
    def test_late_fill_reduces_order_cash_budget_by_business_time(self):
        result = self.run_ledger("fill", "--id", "late", "--at", "2026-10-31T09:45:00Z",
                                 "--recorded-at", "2026-10-31T11:00:00Z", "--symbol", "563360",
                                 "--side", "buy", "--quantity", "90", "--price", "10")
        self.assertEqual(result.returncode, 0, result.stderr)
        order = self.cli("record", "--id", "over-budget", "--symbol", "563360",
                         "--side", "buy", "--quantity", "20", "--price", "10")
        self.assertEqual(order.returncode, 2)
        self.assertIn("insufficient unreserved cash", order.stderr)

    def test_active_orders_reserve_cash_and_holdings_until_terminal(self):
        fill = self.run_ledger("fill", "--id", "fill-1", "--at", "2026-10-31T10:00:00Z",
                               "--symbol", "563360", "--side", "buy", "--quantity", "2", "--price", "10")
        self.assertEqual(fill.returncode, 0, fill.stderr)
        snapshot = subprocess.run([sys.executable, str(ROOT / "snapshots.py"), "--db", str(self.db),
                                   "capture", "--id", "before-orders", "--at", "2026-10-31T10:00:00Z",
                                   "--cash", "980", "--holding", "563360=102",
                                   "--sellable-holding", "563360=100", "--t1-restricted-holding", "563360=2",
                                   "--fills-through", "2026-10-31T10:00:00Z", "--orders-scope-known"],
                                  text=True, capture_output=True)
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
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

        unaudited_terminal = self.cli("status", "--id", "buy-1", "--status", "filled")
        self.assertEqual(unaudited_terminal.returncode, 2)
        inconsistent_filled = self.cli("status", "--id", "buy-1", "--status", "filled",
                                       "--broker-confirmed-terminal", "--filled-quantity", "19")
        self.assertEqual(inconsistent_filled.returncode, 2)
        self.assertIn("cumulative fills must reconcile", inconsistent_filled.stderr)
        confirmed_terminal = self.cli("status", "--id", "buy-1", "--status", "filled",
                                      "--broker-confirmed-terminal", "--filled-quantity", "20")
        self.assertEqual(confirmed_terminal.returncode, 0, confirmed_terminal.stderr)

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

    def test_sell_orders_require_and_respect_confirmed_sellability(self):
        missing = self.cli("record", "--id", "missing-resource", "--symbol", "563360",
                           "--side", "sell", "--quantity", "1", "--price", "10")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("snapshot", missing.stderr)
        self.snapshot_for_risk()
        self.assertEqual(self.cli("record", "--id", "sell-one", "--symbol", "563360", "--side", "sell",
                                  "--quantity", "10", "--price", "10").returncode, 0)
        excess = self.cli("record", "--id", "sell-two", "--symbol", "563360", "--side", "sell",
                          "--quantity", "91", "--price", "10")
        self.assertEqual(excess.returncode, 2)
        self.assertIn("insufficient broker-confirmed sellable quantity", excess.stderr)

    def test_partial_order_reserves_only_remaining_and_unavailable_resources_are_rejected(self):
        bad = self.cli("record", "--id", "too-much", "--symbol", "563360", "--side", "buy",
                       "--quantity", "101", "--price", "10")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("insufficient unreserved cash", bad.stderr)
        self.snapshot_for_risk()
        self.assertEqual(self.cli("record", "--id", "partial-1", "--symbol", "563360", "--side", "sell",
                                  "--quantity", "80", "--price", "12").returncode, 0)
        partial = self.cli("status", "--id", "partial-1", "--status", "partial",
                           "--remaining-quantity", "25")
        self.assertEqual(partial.returncode, 0, partial.stderr)
        active = self.cli("active")
        self.assertIn("partial-1 partial 563360 sell 25 @ 12.00; reserves holdings 25", active.stdout)
        unaudited = self.cli("status", "--id", "partial-1", "--status", "filled",
                             "--remaining-quantity", "0")
        self.assertEqual(unaudited.returncode, 2)
        self.assertIn("terminal order status requires", unaudited.stderr)
        done = self.cli("status", "--id", "partial-1", "--status", "filled",
                        "--broker-confirmed-terminal", "--filled-quantity", "80")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("partial-1", self.cli("active").stdout)
        self.assertEqual(self.cli("record", "--id", "partial-reject", "--symbol", "563360",
                                  "--side", "sell", "--quantity", "10", "--price", "12").returncode, 0)
        self.assertEqual(self.cli("status", "--id", "partial-reject", "--status", "partial",
                                  "--remaining-quantity", "5").returncode, 0)
        inconsistent = self.cli("status", "--id", "partial-reject", "--status", "rejected",
                                "--broker-confirmed-terminal", "--filled-quantity", "0")
        self.assertEqual(inconsistent.returncode, 2)
        rejected = self.cli("status", "--id", "partial-reject", "--status", "rejected",
                            "--broker-confirmed-terminal", "--filled-quantity", "5")
        self.assertEqual(rejected.returncode, 0, rejected.stderr)
    def test_risk_decision_allows_exact_budget_boundary_with_explanation(self):
        self.reanchor_for_risk("250000")
        snapshot = self.snapshot_for_risk(cash="250000")
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        result = self.risk_cli("240")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("new entry: ALLOWED", result.stdout)
        self.assertIn("candidate risk: 240.00 / 240.00", result.stdout)

    def test_entry_keeps_160000_cash_reserve_after_candidate(self):
        self.reanchor_for_risk("200000")
        snapshot = self.snapshot_for_risk(cash="200000")
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        result = self.risk_cli("240")
        self.assertIn("new entry: BLOCKED", result.stdout)
        self.assertIn("required 160000.00 yuan cash reserve", result.stdout)

    def test_effective_ungrouped_fills_increase_existing_risk_exposure(self):
        self.reanchor_for_risk("250000")
        self.assertEqual(self.snapshot_for_risk(cash="250000").returncode, 0)
        fill = self.run_ledger("fill", "--id", "unpaired", "--at", "2026-10-31T10:15:00Z",
                               "--symbol", "563360", "--side", "buy", "--quantity", "10", "--price", "10")
        self.assertEqual(fill.returncode, 0, fill.stderr)
        result = self.risk_cli("0.05")
        self.assertIn("candidate risk: 0.55 / 240.00", result.stdout)
    def test_grouped_loss_and_recorded_fees_remain_in_existing_risk(self):
        self.reanchor_for_risk("250000")
        self.assertEqual(self.snapshot_for_risk(cash="250000").returncode, 0)
        for fill_id, side, price, at in (("group-buy", "buy", "10", "10:10:00"),
                                         ("group-sell", "sell", "9", "10:20:00")):
            self.assertEqual(self.run_ledger("fill", "--id", fill_id,
                                             "--at", f"2026-10-31T{at}Z", "--symbol", "563360",
                                             "--side", side, "--quantity", "10", "--price", price).returncode, 0)
        self.assertEqual(self.run_module("groups.py", "fee", "--fill-id", "group-buy", "--amount", "0.10").returncode, 0)
        self.assertEqual(self.run_module("groups.py", "fee", "--fill-id", "group-sell", "--amount", "0.10").returncode, 0)
        for fill_id in ("group-buy", "group-sell"):
            self.assertEqual(self.run_module("groups.py", "add", "--group", "loss",
                                             "--direction", "reverse", "--fill-id", fill_id).returncode, 0)
        risk = self.risk_cli("0.05")
        self.assertIn("candidate risk: 10.25 / 240.00", risk.stdout)

    def test_daily_stop_uses_shanghai_business_date(self):
        self.db = Path(self.temp.name) / "shanghai.sqlite"
        anchor = self.run_ledger("anchor", "--effective-at", "2026-10-30T15:00:00Z", "--cash", "1000",
                                 "--holding", "563360=100", "--included-through", "2026-10-30T14:59:59Z")
        self.assertEqual(anchor.returncode, 0, anchor.stderr)
        for fill_id, side, price, at in (("sh-buy", "buy", "40", "16:10:00"),
                                         ("sh-sell", "sell", "10", "16:20:00")):
            self.assertEqual(self.run_ledger("fill", "--id", fill_id, "--at", f"2026-10-30T{at}Z",
                                             "--symbol", "563360", "--side", side,
                                             "--quantity", "10", "--price", price).returncode, 0)
        snap = subprocess.run([sys.executable, str(ROOT / "snapshots.py"), "--db", str(self.db), "capture",
                               "--id", "shanghai", "--at", "2026-10-30T17:00:00Z", "--cash", "700",
                               "--holding", "563360=100", "--sellable-holding", "563360=100",
                               "--t1-restricted-holding", "563360=0", "--fills-through", "2026-10-30T17:00:00Z",
                               "--orders-scope-known"], text=True, capture_output=True)
        self.assertEqual(snap.returncode, 0, snap.stderr)
        result = self.risk_cli("0.05")
        self.assertIn("reaching -300.00 yuan", result.stdout)
    def test_risk_stops_above_budget_and_at_daily_loss_threshold(self):
        self.reset_account("300000")
        snapshot = subprocess.run([sys.executable, str(ROOT / "snapshots.py"), "--db", str(self.db),
                                   "capture", "--id", "s1", "--at", "2026-10-31T10:00:00Z",
                                   "--cash", "300000", "--holding", "563360=100",
                                   "--sellable-holding", "563360=100", "--t1-restricted-holding", "563360=0",
                                   "--fills-through", "2026-10-31T10:00:00Z", "--orders-scope-known"],
                                  text=True, capture_output=True)
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        result = self.risk_cli("240")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("new entry: ALLOWED", result.stdout)
        self.assertIn("candidate risk: 240.00 / 240.00", result.stdout)
        self.snapshot_for_risk()
        invalid = subprocess.run([sys.executable, str(ROOT / "snapshots.py"), "--db", str(self.db),
                                  "risk", "--market-price", "NaN", "--candidate-quantity", "1",
                                  "--fee-rate", "0", "--action", "entry"], text=True, capture_output=True)
        self.assertNotEqual(invalid.returncode, 0)
        malformed = subprocess.run([sys.executable, str(ROOT / "snapshots.py"), "--db", str(self.db),
                                    "risk", "--market-price", "2", "--candidate-quantity", "1",
                                    "--fee-rate", "invalid", "--action", "entry"], text=True, capture_output=True)
        self.assertNotEqual(malformed.returncode, 0)
        self.assertNotIn("Traceback", malformed.stderr)
    def test_reconciliation_blocks_entries_but_keeps_exit_and_maintenance_available(self):
        snapshot = subprocess.run([sys.executable, str(ROOT / "snapshots.py"), "--db", str(self.db),
                                   "capture", "--id", "s1", "--at", "2026-10-31T10:00:00Z",
                                   "--cash", "999", "--holding", "563360=100",
                                   "--fills-through", "2026-10-31T10:00:00Z", "--orders-scope-known"],
                                  text=True, capture_output=True)
        self.assertIn("MISMATCH", snapshot.stdout)
        blocked = self.risk_cli("1")
        self.assertIn("new entry: BLOCKED", blocked.stdout)
        self.assertIn("resource projection is blocked", blocked.stdout)
        for action in ("exit", "maintenance"):
            allowed = self.risk_cli("1", action)
            self.assertIn("new entry: BLOCKED", allowed.stdout)
            self.assertIn("action status: AVAILABLE", allowed.stdout)
            self.assertIn("remain available", allowed.stdout)

    def test_daily_stop_remains_locked_after_late_corrected_fill_and_rebuild(self):
        self.snapshot_for_risk()
        self.record_loss_round_trip()
        stopped = self.risk_cli("1")
        self.assertIn("new entry: BLOCKED", stopped.stdout)
        self.assertIn("reaching -300.00 yuan", stopped.stdout)
        corrected = self.run_ledger("correct", "--id", "loss-sell-corrected",
                                    "--at", "2026-10-31T10:06:00Z", "--symbol", "563360",
                                    "--side", "sell", "--quantity", "10", "--price", "40",
                                    "--supersedes", "loss-sell")
        self.assertEqual(corrected.returncode, 0, corrected.stderr)
        rebuild = self.run_ledger("rebuild", "--confirm-rebuild")
        self.assertEqual(rebuild.returncode, 0, rebuild.stderr)
        self.assertIn("before cash: 1000.00", rebuild.stdout)
        self.assertIn("after cash: 1000.00", rebuild.stdout)
        recovered = self.risk_cli("1")
        self.assertIn("new entry: BLOCKED", recovered.stdout)
        self.assertIn("stop remains locked", recovered.stdout)

    def record_loss_round_trip(self):
        buy = self.run_ledger("fill", "--id", "loss-buy", "--at", "2026-10-31T10:05:00Z",
                              "--symbol", "563360", "--side", "buy", "--quantity", "10", "--price", "40")
        sell = self.run_ledger("fill", "--id", "loss-sell", "--at", "2026-10-31T10:06:00Z",
                               "--symbol", "563360", "--side", "sell", "--quantity", "10", "--price", "10")
        self.assertEqual(buy.returncode, 0, buy.stderr)
        self.assertEqual(sell.returncode, 0, sell.stderr)
    def reanchor_for_risk(self, cash):
        result = self.run_ledger("reanchor", "--confirm-reanchor",
                                 "--effective-at", "2026-10-31T09:59:00Z", "--cash", cash,
                                 "--holding", "563360=100", "--included-through", "2026-10-31T09:59:00Z")
        self.assertEqual(result.returncode, 0, result.stderr)

    def snapshot_for_risk(self, cash="1000"):
        result = subprocess.run([sys.executable, str(ROOT / "snapshots.py"), "--db", str(self.db),
                                 "capture", "--id", "s1", "--at", "2026-10-31T10:00:00Z",
                                 "--cash", cash, "--holding", "563360=100",
                                 "--sellable-holding", "563360=100", "--t1-restricted-holding", "563360=0",
                                 "--fills-through", "2026-10-31T10:00:00Z", "--orders-scope-known"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def risk_cli(self, candidate_risk, action="entry"):
        cents = int(round(float(candidate_risk) * 100))
        quantity = (cents + 4) // 5
        return subprocess.run([sys.executable, str(ROOT / "snapshots.py"), "--db", str(self.db),
                               "risk", "--market-price", "10", "--candidate-quantity", str(quantity),
                               "--fee-rate", "0", "--action", action], text=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
