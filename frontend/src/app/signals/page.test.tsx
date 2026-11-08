import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as Client from "@/lib/a500-client";
const { getSignalSnapshot, streamSignals } = vi.hoisted(() => ({
  getSignalSnapshot: vi.fn(),
  streamSignals: vi.fn(),
}));
vi.mock("@/lib/a500-client", async (importOriginal) => ({
  ...(await importOriginal<typeof Client>()),
  getSignalSnapshot,
  streamSignals,
}));
vi.mock("@/lib/oidc", () => ({ configurationReady: true, beginAuthorization: vi.fn() }));

import type { Signal, SignalEvent } from "@/lib/a500-client";
import { clearAccessToken, setAccessToken } from "@/lib/session";
import SignalsPage from "@/app/signals/page";

const signal = (symbol: string, stale = false): Signal => ({
  schema_version: 1,
  symbol,
  bar_timestamp: "2026-11-06T07:00:00Z",
  strategy_identity: "wide-index",
  rule_version: "2",
  parameters: { lookback: 20 },
  parameter_fingerprint: "sha256:abc",
  status: stale ? "UNAVAILABLE" : "BUY",
  direction: stale ? null : "BUY",
  quantity: stale ? null : 100,
  reasons: ["策略条件满足"],
  stale,
  execution_eligible: !stale,
});

let streamCallback: ((event: SignalEvent) => void) | undefined;
beforeEach(() => {
  vi.clearAllMocks();
  streamCallback = undefined;
  setAccessToken("memory-token", 300);
  getSignalSnapshot.mockResolvedValue({
    signals: [signal("510500"), signal("159919", true)],
    strategy: "wide-index",
    parameter_fingerprint: "sha256:abc",
  });
  streamSignals.mockImplementation(
    (
      _token: string,
      _symbols: string[],
      _abort: AbortSignal,
      callback: (event: SignalEvent) => void,
    ) => {
      streamCallback = callback;
      return new Promise<void>(() => undefined);
    },
  );
});
afterEach(() => {
  cleanup();
  clearAccessToken();
});

describe("signals route", () => {
  it("loads real snapshot data and never presents stale or unavailable values as current suggestions", async () => {
    render(<SignalsPage />);
    expect(await screen.findByRole("row", { name: /510500/ })).toBeTruthy();
    expect(getSignalSnapshot).toHaveBeenCalledWith(
      "memory-token",
      undefined,
      expect.any(AbortSignal),
    );
    expect(screen.getByText("不作为当前建议")).toBeTruthy();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
    expect(screen.getByText("不具备执行资格")).toBeTruthy();
    expect(screen.getAllByText("sha256:abc").length).toBeGreaterThan(0);
    expect(screen.getAllByText("非下单指令").length).toBeGreaterThan(0);
  });
  it("preserves the signal record's accessible table order", async () => {
    render(<SignalsPage />);
    const row = await screen.findByRole("row", { name: /510500/ });
    const cells = within(row).getAllByRole("cell");
    expect(cells).toHaveLength(5);
    expect(cells[0]?.textContent).toContain("偏多参考");
    expect(cells[1]?.textContent).toBe("100");
    expect(cells[2]?.textContent).toBe("策略条件满足");
    expect(cells[3]?.querySelector("time")?.textContent).toContain("2026");
    expect(cells[4]?.textContent).toContain("非下单指令");
  });

  it("requests the selected symbols and refetches current data on reset", async () => {
    render(<SignalsPage />);
    await screen.findByRole("row", { name: /510500/ });
    fireEvent.click(screen.getAllByText(/全部标的 · 2/)[0]);
    fireEvent.click(screen.getByLabelText("159919"));
    await waitFor(() =>
      expect(getSignalSnapshot).toHaveBeenLastCalledWith("memory-token", ["159919"], undefined),
    );
    await waitFor(() =>
      expect(streamSignals).toHaveBeenLastCalledWith(
        "memory-token",
        ["159919"],
        expect.any(AbortSignal),
        expect.any(Function),
        undefined,
      ),
    );
    await act(async () => streamCallback?.({ event: "reset", data: { reason: "cursor_expired" } }));
    await waitFor(() =>
      expect(getSignalSnapshot).toHaveBeenLastCalledWith(
        "memory-token",
        ["159919"],
        expect.any(AbortSignal),
      ),
    );
  });
});
