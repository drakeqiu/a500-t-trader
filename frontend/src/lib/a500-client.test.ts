import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  abortableDelay,
  confirmPreview,
  getNav,
  getPreview,
  getSignals,
  getSnapshot,
  parseSignalEvent,
  pollDeviceToken,
  saveSnapshot,
  streamSignals,
} from "./a500-client";
import { parseSnapshotDraft, snapshotDraft } from "./snapshot-draft";
import type { AccountSnapshot } from "./a500-client";

const snapshot: AccountSnapshot = {
  as_of: "2026-11-06T15:00:00+08:00",
  available_cash: "0.000000000000000001",
  holdings: { "510500": 100, "159919": 200 },
  protected_floor: { "510500": 0, "159919": 0 },
  sellable: { "510500": 100, "159919": 200 },
  cost_basis: { "510500": "5.123456789123456789", "159919": "4.00" },
};
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
describe("account API", () => {
  it("preserves decimal strings, timezone and multiple symbol mappings", () => {
    expect(parseSnapshotDraft(snapshotDraft(snapshot))).toEqual(snapshot);
    expect(() =>
      parseSnapshotDraft({ ...snapshotDraft(snapshot), as_of: "2026-11-06T15:00:00" }),
    ).toThrow(/时区/);
    expect(() =>
      parseSnapshotDraft({ ...snapshotDraft(snapshot), cost_basis: '{"510500":5.123}' }),
    ).toThrow(/字符串/);
  });
  it("uses same-origin API and explicit creation/replacement preconditions", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(async () => Response.json(snapshot, { headers: { ETag: '"v2"' } }));
    vi.stubGlobal("fetch", fetchMock);
    await saveSnapshot("token", snapshot, null);
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/account-snapshot",
      expect.objectContaining({
        method: "PUT",
        headers: expect.objectContaining({ "If-None-Match": "*", Authorization: "Bearer token" }),
        body: JSON.stringify(snapshot),
      }),
    );
    await saveSnapshot("token", snapshot, '"v1"');
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/account-snapshot",
      expect.objectContaining({ headers: expect.objectContaining({ "If-Match": '"v1"' }) }),
    );
  });
  it("propagates 412, 428 and 401 with typed status for draft/auth handling", async () => {
    for (const status of [412, 428, 401]) {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(Response.json({ title: "Conflict" }, { status })),
      );
      await expect(saveSnapshot("token", snapshot, '"old"')).rejects.toMatchObject({ status });
    }
  });
  it("does not turn NAV transport/auth errors into a fabricated unavailable result", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json({ detail: "Unauthorized" }, { status: 401 })),
    );
    await expect(getNav("token")).rejects.toBeInstanceOf(ApiError);
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          Response.json(
            { status: "unavailable", nav: null, reason: "account_snapshot_missing" },
            { status: 404 },
          ),
        ),
    );
    await expect(getNav("token")).resolves.toMatchObject({ reason: "account_snapshot_missing" });
  });
  it("requires an ETag to safely edit an existing snapshot", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(snapshot)));
    await expect(getSnapshot("token")).rejects.toThrow(/ETag/);
  });
  it("recovers batch details from GET after confirm's ledger-only response", async () => {
    const preview = {
      batch_id: "batch-1",
      valid: 1,
      new: 1,
      duplicates: 0,
      errors: 0,
      confirmed: { imported: 1 },
      error_details: [],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ imported: 1 }))
      .mockResolvedValueOnce(Response.json(preview));
    vi.stubGlobal("fetch", fetchMock);
    await expect(confirmPreview("token", "batch-1")).resolves.toEqual(preview);
    expect(fetchMock.mock.calls[1][0]).toBe("/api/v1/trade-imports/previews/batch-1");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json({ detail: "Preview not found" }, { status: 404 })),
    );
    await expect(getPreview("other-token", "batch-1")).rejects.toMatchObject({ status: 404 });
  });
});
describe("SSE protocol", () => {
  it("parses named reset events, ids, comments and backend directional envelopes", () => {
    expect(parseSignalEvent(': keepalive\r\nevent: reset\r\ndata: {"reason":"expired"}')).toEqual({
      event: "reset",
      data: { reason: "expired" },
    });
    expect(
      parseSignalEvent(
        'id: 12-0\nevent: buy\ndata: {"event_id":"12-0",\ndata: "signal":{"symbol":"510500"}}',
      ),
    ).toEqual({
      event: "buy",
      id: "12-0",
      data: { event_id: "12-0", signal: { symbol: "510500" } },
    });
    expect(parseSignalEvent(": heartbeat")).toBeNull();
    expect(parseSignalEvent("id:\ndata: {}")).toEqual({ event: "message", id: "", data: {} });
  });
  it("handles chunk-split CRLF/UTF-8 and sends the reconnect cursor", async () => {
    const bytes = new TextEncoder().encode(
      'id: 12-0\r\nevent: buy\r\ndata: {"signal":{"reason":"中文"}}\r\n\r\nevent: reset\ndata: {"reason":"expired"}\n\n',
    );
    const response = new Response(
      new ReadableStream<Uint8Array>({
        start(controller) {
          for (let index = 0; index < bytes.length; index += 3)
            controller.enqueue(bytes.slice(index, index + 3));
          controller.close();
        },
      }),
    );
    const fetchMock = vi.fn().mockResolvedValue(response);
    vi.stubGlobal("fetch", fetchMock);
    const onEvent = vi.fn();
    await streamSignals(
      "token",
      ["510500", "159919"],
      new AbortController().signal,
      onEvent,
      "11-0",
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/signals/stream?symbols=510500&symbols=159919",
      expect.objectContaining({ headers: expect.objectContaining({ "Last-Event-ID": "11-0" }) }),
    );
    expect(onEvent.mock.calls.map(([event]) => event)).toEqual([
      { event: "buy", id: "12-0", data: { signal: { reason: "中文" } } },
      { event: "reset", data: { reason: "expired" } },
    ]);
  });
  it("skips empty subscriptions and propagates stream authorization failures", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(Response.json({ detail: "Unauthorized" }, { status: 401 }));
    vi.stubGlobal("fetch", fetchMock);
    await streamSignals("token", [], new AbortController().signal, vi.fn());
    expect(fetchMock).not.toHaveBeenCalled();
    await expect(
      streamSignals("token", ["510500"], new AbortController().signal, vi.fn()),
    ).rejects.toMatchObject({ status: 401 });
    await expect(getSignals("token")).rejects.toMatchObject({ status: 401 });
  });
});
describe("device authorization cancellation", () => {
  it("cancels the interval without issuing a token request", async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const pending = pollDeviceToken("/token", "device", 5, controller.signal);
    const rejection = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    controller.abort();
    await rejection;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(fetchMock).not.toHaveBeenCalled();
    await expect(abortableDelay(1000, controller.signal)).rejects.toMatchObject({
      name: "AbortError",
    });
  });
});
