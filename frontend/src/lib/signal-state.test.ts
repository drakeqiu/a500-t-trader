import { describe, expect, it } from "vitest";
import type { Signal } from "./a500-client";
import { mergeSignal, signalFromEvent } from "./signal-state";

const row = (symbol: string, direction: string | null, stale = false): Signal => ({
  schema_version: 1,
  symbol,
  bar_timestamp: "2026-11-06T07:00:00Z",
  strategy_identity: "strategy",
  rule_version: "1",
  parameters: {},
  parameter_fingerprint: "hash",
  status: stale ? "UNAVAILABLE" : direction ?? "WAIT",
  direction: stale ? null : direction,
  quantity: stale ? null : direction ? 100 : null,
  reasons: [],
  stale,
  execution_eligible: !stale,
});

describe("signal stream state", () => {
  it("accepts snapshot and directional payload envelopes", () => {
    expect(signalFromEvent(row("510500", "BUY"))?.symbol).toBe("510500");
    expect(signalFromEvent({ signal: row("159919", "SELL") })?.direction).toBe("SELL");
    expect(signalFromEvent({ reason: "expired" })).toBeNull();
  });

  it("replaces only the matching symbol and preserves unavailable nulls", () => {
    const original = [row("510500", "BUY"), row("159919", "SELL")];
    const next = mergeSignal(original, row("510500", null, true));
    expect(next.map(({ symbol, direction, quantity, stale }) => ({ symbol, direction, quantity, stale }))).toEqual([
      { symbol: "510500", direction: null, quantity: null, stale: true },
      { symbol: "159919", direction: "SELL", quantity: 100, stale: false },
    ]);
    expect(mergeSignal(original, row("513100", "BUY"))).toHaveLength(3);
  });
});
