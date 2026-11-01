# Keep vendor APIs behind a replaceable market-data provider

## Status

Accepted

The market-data service uses a vendor-neutral contract and keeps AmazingData
and its underlying `tgw` APIs inside a replaceable provider. In internet mode,
the provider combines AmazingData login/query and calendar APIs with `tgw`
subscription APIs. Query results are not represented as push events, and
unavailable or unauthorized capabilities are reported explicitly rather than
simulated.

The Shanghai trading calendar is an injectable dependency, adapted from
AmazingData `BaseData.get_calendar(market='SH')` when available. Otherwise, the
caller supplies another calendar implementation. Calendar failure never
implies that the market is open.

For push consumers, the design offers callback and iterator access. Snapshot
and intraday-bar events coalesce to the latest value when an iterator buffer
is full. Tick events are not silently dropped, and overflow is surfaced by
terminating the affected subscription with an explicit error. Intraday bar
updates distinguish an in-progress minute from a completed bar.

Calendar data is fetched at startup and refreshed once per Shanghai-local
date. Failed refresh retains the last valid calendar as stale, while absence
of any valid calendar leaves session status unknown.
