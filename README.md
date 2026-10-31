# a500-t-trader

> **Offline bookkeeping only — not connected to a broker and not for live trading.** Commands record user-confirmed facts and calculate conservative projections; they never submit orders or provide live broker verification. Do not use for trading until broker rules are documented, a broker SDK is integrated, and live behavior is tested and reviewed.

## Local account ledger

The CLI stores a confirmed account anchor and broker-confirmed fills in a local SQLite database. Times must be ISO-8601 with a timezone; amounts use yuan with at most two decimal places; quantities are whole ETF shares. The anchor's `--included-through` timestamp is inclusive: fills at or before it are retained for traceability but do not change the anchored cash or holdings. Each fill's `--id` must be the broker's stable unique fill identity. `--at` is the business occurrence time; optional `--recorded-at` records when it was entered (defaults to UTC now). A later correction is a new fill ID linked with `correct --supersedes`; the old version remains visible but only the current version is projected.

```sh
python3 ledger.py --db account.sqlite anchor \
  --effective-at 2026-10-31T09:30:00+08:00 --cash 10000 \
  --holding 563360=100 --included-through 2026-10-31T09:29:59+08:00
python3 ledger.py --db account.sqlite fill --id broker-fill-123 \
  --at 2026-10-31T10:00:00+08:00 --symbol 563360 --side buy \
  --quantity 10 --price 2.50
python3 ledger.py --db account.sqlite balance
# Correct a previously recorded fill without erasing its original version.
python3 ledger.py --db account.sqlite correct --id broker-fill-123-v2 \
  --supersedes broker-fill-123 --at 2026-10-31T10:00:00+08:00 \
  --recorded-at 2026-10-31T12:00:00+08:00 --symbol 563360 --side buy \
  --quantity 9 --price 2.50
python3 ledger.py --db account.sqlite rebuild --confirm-rebuild
# Re-anchoring replaces the anchor only after explicit confirmation and validates replay.
python3 ledger.py --db account.sqlite reanchor --confirm-reanchor \
  --effective-at 2026-10-31T12:00:00+08:00 --cash 9977.50 \
  --holding 563360=109 --included-through 2026-10-31T11:59:59+08:00
```

`rebuild --confirm-rebuild` transactionally recomputes persisted fill-application flags from the confirmed anchor, then reports the projected account state before and after; the before/after comparison is auditable and base events, snapshots, and risk-stop latches are retained. `reanchor --confirm-reanchor` is explicit and retains the previous anchor and holdings in the audit history shown by `balance`. Run the black-box CLI scenarios with `python3 -m unittest discover -s tests`.

## Shadow account comparison

`shadow.py` uses the same SQLite database and snapshots the projected real account only after explicit confirmation. Real broker fills change the real account only. Confirmed external cash flows and ETF holding transfers are stored as typed `account_events`, applied to the real projection, and mirrored to shadow in one transaction when possible. A holding transfer changes quantity only; it does not invent a sale price or cash amount. Event IDs are stable and idempotent. Each external account adjustment requires `--confirm-adjustment`; `--recorded-at` may be supplied separately from its business `--at`.

```sh
python3 shadow.py --db account.sqlite reset --confirm-reset \
  --at 2026-10-31T09:30:00+08:00
python3 shadow.py --db account.sqlite cash-flow --id deposit-1 \
  --at 2026-10-31T10:00:00+08:00 --direction in --amount 1000 --confirm-adjustment
python3 shadow.py --db account.sqlite holding-adjustment --id transfer-1 \
  --at 2026-10-31T10:05:00+08:00 --symbol 563360 --direction in --quantity 10 --confirm-adjustment
python3 shadow.py --db account.sqlite compare --price 563360=2.60
```

`compare` reports real equity minus shadow equity at the supplied prices; it is an absolute net excess in yuan, not an annualized return. Supply a price for every held symbol. Cash flows and external holding adjustments are mirrored as confirmed account facts and shown by `ledger.py balance`. If a mirror would make real or shadow cash/holdings negative, the event is rejected atomically; after reconciling the real account, restart the comparison baseline with `reset --confirm-reset`. Reset intentionally starts net excess at zero. This deliberately mirrors only cash flows and explicitly identified holding transfers—not corporate actions or other unknown broker events.

## Broker snapshots and reconciliation

`snapshots.py` records broker observations separately from confirmed ledger facts. Snapshot cash and holdings are total account balances (not available-to-trade balances). `--fills-through` is an inclusive, evidence-backed boundary for fills already reflected in those balances; omission leaves fill coverage unknown. `--orders-scope-known` explicitly confirms that the list of orders represented in broker-frozen resources is complete; `--covered-order` identifies those order IDs. Frozen broker cash/holdings are deducted once, and active uncovered orders are reserved by stable order ID. Unknown scope, resource shortfalls, or a mismatch block opening.

```sh
python3 snapshots.py --db account.sqlite capture --id broker-1 \
  --at 2026-10-31T10:30:00+08:00 --cash 9975 --holding 563360=110 \
  --sellable-holding 563360=100 --t1-restricted-holding 563360=10 \
  --fills-through 2026-10-31T10:00:00+08:00 \
  --orders-scope-known --covered-order buy-1 --frozen-cash 25
python3 snapshots.py --db account.sqlite status
python3 snapshots.py --db account.sqlite resources
```

`capture` reports broker-minus-ledger cash and per-symbol holding deltas; any mismatch explicitly requires reconciliation before openings can be allowed. It never changes ledger balances. Resource projection starts from the snapshot, applies confirmed account cash/holding events strictly after the snapshot once, adds only fills later than their inclusive coverage boundary, deducts broker-reported frozen resources, and subtracts active orders not identified as already frozen. Post-snapshot sell proceeds remain excluded from entry cash. A mismatch is not auto-corrected: reconcile the broker and ledger explicitly before treating openings as allowed. No snapshot history is used as a substitute for a confirmed ledger fact.

`resources` reports broker-confirmed sellable and T+1-restricted quantities independently from projected balances. Supply both `--sellable-holding` and `--t1-restricted-holding` from broker evidence for every held security. Confirmed buys after the inclusive fill boundary increase T+1-restricted holdings; confirmed sells reduce sellable holdings. Orders cannot sell more than the remaining broker-confirmed sellable amount after frozen resources and reservations. The CLI never infers T+1 rollover from weekdays; missing sellability evidence blocks sells and openings.

## New-entry risk decision

Risk includes effective grouped and ungrouped fills, group losses and actual fees, active-order exposure, and candidate market value with conservative stress/fee assumptions. The entry gate retains at least ¥160,000 cash after each candidate. Daily realized P&L is recomputed from effective persisted fills and actual recorded fees (FIFO matching, Asia/Shanghai business dates). Crossing -¥300 latches that Shanghai date in SQLite, including when late/corrected effective fills change its result. Exactly ¥240 risk is within the limit; exactly -¥300 realized daily P&L stops entry. Missing or invalid market inputs and unreconciled resources conservatively block new entries.

```sh
python3 snapshots.py --db account.sqlite risk \
  --market-price 2.50 --candidate-quantity 1000 --fee-rate 0.0003 --action entry
```

The persisted stop remains active after P&L recovers or projections are rebuilt. Exits and accounting maintenance remain available while entry is stopped, but an exit must specify `--symbol` and is available only when a reconciled resource projection confirms enough sellable holdings after T+1 restrictions, broker freezes, and active order reservations. For example, add `--action exit --symbol 563360` to assess an exit; exit capacity does not use the new-entry cash reserve or risk cap. Market price and fee rate must be current trusted inputs; uncertain or unavailable inputs block new entries.

## Actual fees and T groups

Record the broker's actual fee against its confirmed fill ID, then attach fills to a positive T (sell followed by buy) or reverse T (buy followed by sell) group. Fees are optional; no fee is assumed when one has not been recorded. A fee may be entered once per fill (an identical retry is idempotent), and a fill may belong to only one group. Group reports use fill quantities/prices and recorded fees, show paired net P&L separately from remaining buy/sell exposure, and retain each fill and fee as auditable detail. A confirmed fee reduces projected account cash and holdings availability.

```sh
python3 groups.py --db account.sqlite fee --fill-id broker-fill-123 --amount 0.05
python3 groups.py --db account.sqlite add --group morning-1 --direction reverse --fill-id broker-fill-123
python3 groups.py --db account.sqlite add --group morning-1 --direction reverse --fill-id broker-fill-124
python3 groups.py --db account.sqlite report --group morning-1
```
