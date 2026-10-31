# a500-t-trader

## Local account ledger

The CLI stores a confirmed account anchor and broker-confirmed fills in a local SQLite database. Times must be ISO-8601 with a timezone; amounts use yuan with at most two decimal places; quantities are whole ETF shares. The anchor's `--included-through` timestamp is inclusive: fills at or before it are retained for traceability but do not change the anchored cash or holdings. Each fill's `--id` must be the broker's stable unique fill identity.

```sh
python3 ledger.py --db account.sqlite anchor \
  --effective-at 2026-10-31T09:30:00+08:00 --cash 10000 \
  --holding 563360=100 --included-through 2026-10-31T09:29:59+08:00
python3 ledger.py --db account.sqlite fill --id broker-fill-123 \
  --at 2026-10-31T10:00:00+08:00 --symbol 563360 --side buy \
  --quantity 10 --price 2.50
python3 ledger.py --db account.sqlite balance
```

A fill that would create negative cash or holdings is rejected without recording the fill. Repeating the same fill identity and facts is idempotent; reusing an identity for different facts is rejected. Run the black-box CLI scenarios with `python3 -m unittest discover -s tests`.

## Actual fees and T groups

Record the broker's actual fee against its confirmed fill ID, then attach fills to a positive T (sell followed by buy) or reverse T (buy followed by sell) group. Fees are optional; no fee is assumed when one has not been recorded. A fee may be entered once per fill (an identical retry is idempotent), and a fill may belong to only one group. Group reports use fill quantities/prices and recorded fees, show paired net P&L separately from remaining buy/sell exposure, and retain each fill and fee as auditable detail. This reporting does not alter ledger cash or holdings.

```sh
python3 groups.py --db account.sqlite fee --fill-id broker-fill-123 --amount 0.05
python3 groups.py --db account.sqlite add --group morning-1 --direction reverse --fill-id broker-fill-123
python3 groups.py --db account.sqlite add --group morning-1 --direction reverse --fill-id broker-fill-124
python3 groups.py --db account.sqlite report --group morning-1
```
