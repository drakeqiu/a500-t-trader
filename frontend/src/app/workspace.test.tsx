import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/a500-client", async (importOriginal) => ({
  ...(await importOriginal<typeof client>()),
  getNav: vi.fn(),
  getSignals: vi.fn(),
  getSnapshot: vi.fn(),
  saveSnapshot: vi.fn(),
  getPreview: vi.fn(),
  uploadPreview: vi.fn(),
  confirmPreview: vi.fn(),
  streamSignals: vi.fn(),
}));
vi.mock("@/lib/oidc", () => ({
  configurationReady: true,
  beginAuthorization: vi.fn(),
  completeAuthorization: vi.fn(),
}));
import * as oidc from "@/lib/oidc";
import * as client from "@/lib/a500-client";
import Workspace from "@/app/workspace";

const snapshot: client.AccountSnapshot = {
  as_of: "2026-11-06T15:00:00+08:00",
  available_cash: "123.4500",
  holdings: { "510500": 100, "159919": 200 },
  protected_floor: { "510500": 0, "159919": 100 },
  sellable: { "510500": 100, "159919": 100 },
  cost_basis: { "510500": "5.1234567890123456789", "159919": "4.2" },
};
const signal: client.Signal = {
  schema_version: 1,
  symbol: "510500",
  bar_timestamp: "2026-11-06T07:00:00Z",
  strategy_identity: "test",
  rule_version: "1",
  parameters: {},
  parameter_fingerprint: "test",
  status: "UNAVAILABLE",
  direction: null,
  quantity: null,
  reasons: ["quote stale"],
  stale: true,
  execution_eligible: false,
};
const preview: client.ImportPreview = {
  batch_id: "batch-1",
  valid: 1,
  new: 1,
  duplicates: 0,
  errors: 0,
  error_details: [],
};
let stream: PromiseWithResolvers<void>;

beforeEach(() => {
  vi.resetAllMocks();
  window.localStorage.clear();
  stream = Promise.withResolvers<void>();
  vi.mocked(oidc.completeAuthorization).mockReturnValue(null);
  vi.mocked(client.getSignals).mockResolvedValue([signal]);
  vi.mocked(client.getNav).mockResolvedValue({
    status: "unavailable",
    nav: null,
    reason: "quote_stale",
  });
  vi.mocked(client.getSnapshot).mockResolvedValue({ snapshot, etag: '"v1"' });
  vi.mocked(client.getPreview).mockResolvedValue(preview);
  vi.mocked(client.streamSignals).mockImplementation(() => stream.promise);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
async function login() {
  vi.mocked(oidc.completeAuthorization).mockReturnValue(Promise.resolve({ token: "memory-token", expiresIn: 300 }));
  render(<Workspace />);
  await screen.findByRole("heading", { name: "账户快照" });
  await waitFor(() =>
    expect(screen.getByLabelText("可用现金")).toHaveProperty("value", "123.4500"),
  );
}

it("disables connection in server HTML until the client has hydrated", () => {
  const html = renderToString(<Workspace />);
  const document = new DOMParser().parseFromString(html, "text/html");
  const button = [...document.querySelectorAll("button")].find((item) =>
    item.textContent?.includes("安全连接"),
  );
  expect(button?.disabled).toBe(true);
  render(<Workspace />);
  expect(screen.getByRole("button", { name: "安全连接" })).toHaveProperty("disabled", false);
});

it("reports callback errors without displaying the workspace", async () => {
  vi.mocked(oidc.completeAuthorization).mockReturnValue(Promise.reject(new Error("授权已拒绝。")));
  render(<Workspace />);
  expect((await screen.findByRole("alert")).textContent).toContain("授权已拒绝");
  expect(screen.queryByRole("heading", { name: "账户快照" })).toBeNull();
});

it("clears the in-memory session when the access token expires", async () => {
  vi.mocked(oidc.completeAuthorization).mockReturnValue(Promise.resolve({ token: "memory-token", expiresIn: 0.1 }));
  render(<Workspace />);
  await screen.findByRole("heading", { name: "账户快照" });
  expect((await screen.findByRole("alert")).textContent).toContain("授权已过期");
  expect(screen.queryByRole("heading", { name: "账户快照" })).toBeNull();
});

describe("snapshot workflows", () => {
  it.each([412, 428])(
    "retains all draft fields on %i and requires an explicit latest-version choice",
    async (status) => {
      vi.mocked(client.saveSnapshot).mockRejectedValue(
        new client.ApiError(status, "version mismatch"),
      );
      await login();
      fireEvent.change(screen.getByLabelText("可用现金"), { target: { value: "999.000001" } });
      fireEvent.click(screen.getByRole("button", { name: "保存账户快照" }));
      await screen.findByText(/快照版本冲突，草稿已保留/);
      expect(client.saveSnapshot).toHaveBeenCalledWith(
        "memory-token",
        { ...snapshot, available_cash: "999.000001" },
        '"v1"',
        expect.any(AbortSignal),
      );
      vi.mocked(client.getSnapshot).mockResolvedValue({
        snapshot: { ...snapshot, available_cash: "777" },
        etag: '"v2"',
      });
      fireEvent.click(screen.getByRole("button", { name: "读取最新快照" }));
      await screen.findByRole("button", { name: "使用最新快照替换草稿" });
      expect(screen.getByLabelText("可用现金")).toHaveProperty("value", "999.000001");
      expect(screen.getByRole("button", { name: "保存账户快照" })).toHaveProperty("disabled", true);
      fireEvent.click(screen.getByRole("button", { name: "使用最新快照替换草稿" }));
      expect(screen.getByLabelText("可用现金")).toHaveProperty("value", "777");
      expect(screen.getByRole("button", { name: "保存账户快照" })).toHaveProperty(
        "disabled",
        false,
      );
    },
  );
  it("creates with an absent-snapshot precondition and refreshes NAV after save", async () => {
    vi.mocked(client.getSnapshot).mockResolvedValue(null);
    vi.mocked(client.saveSnapshot).mockResolvedValue({ snapshot, etag: '"created"' });
    vi.mocked(oidc.completeAuthorization).mockReturnValue(Promise.resolve({ token: "memory-token", expiresIn: 300 }));
    render(<Workspace />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "保存账户快照" })).toHaveProperty(
        "disabled",
        false,
      ),
    );
    fireEvent.change(screen.getByLabelText("快照时间（含时区）"), {
      target: { value: snapshot.as_of },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存账户快照" }));
    await waitFor(() =>
      expect(client.saveSnapshot).toHaveBeenCalledWith(
        "memory-token",
        expect.objectContaining({ holdings: {}, cost_basis: {}, available_cash: "0" }),
        null,
        expect.any(AbortSignal),
      ),
    );
    await waitFor(() => expect(client.getNav).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText("可用现金")).toHaveProperty("value", "123.4500");
  });
});
describe("identity and import recovery", () => {
  it("restores a persisted batch through its ownership-checked GET and never persists the token", async () => {
    window.localStorage.setItem("a500.import-batch-id", "batch-1");
    vi.mocked(client.getPreview).mockResolvedValue({ ...preview, confirmed: { imported: 1 } });
    await login();
    await screen.findByRole("heading", { name: "已确认" });
    expect(client.getPreview).toHaveBeenCalledWith(
      "memory-token",
      "batch-1",
      expect.any(AbortSignal),
    );
    expect(window.localStorage.length).toBe(1);
    expect(window.localStorage.getItem("a500.import-batch-id")).toBe("batch-1");
  });
  it("clears inaccessible or expired batch references without exposing another owner's preview", async () => {
    window.localStorage.setItem("a500.import-batch-id", "other-owner");
    vi.mocked(client.getPreview).mockRejectedValue(new client.ApiError(404, "Preview not found"));
    await login();
    await screen.findByText(/导入批次不存在、已过期或不属于当前账户/);
    expect(window.localStorage.getItem("a500.import-batch-id")).toBeNull();
    expect(screen.queryByRole("heading", { name: "导入预览" })).toBeNull();
  });
  it("renders sanitized per-row errors and prevents confirmation", async () => {
    vi.mocked(client.uploadPreview).mockResolvedValue({
      ...preview,
      errors: 1,
      error_details: [{ row: 3, field: "成交数量", reason: "invalid_quantity" }],
    });
    await login();
    fireEvent.change(screen.getByLabelText("选择成交文件"), {
      target: { files: [new File(["csv"], "trades.csv")] },
    });
    fireEvent.click(screen.getByRole("button", { name: "预览文件" }));
    expect(await screen.findByRole("list", { name: "导入逐行错误" })).toHaveProperty(
      "textContent",
      "第 3 行 · 成交数量 · invalid_quantity",
    );
    expect(screen.queryByRole("button", { name: "确认导入" })).toBeNull();
  });
  it("confirms imports without writing or refreshing the independent account snapshot", async () => {
    window.localStorage.setItem("a500.import-batch-id", "batch-1");
    vi.mocked(client.confirmPreview).mockResolvedValue({ ...preview, confirmed: { imported: 1 } });
    await login();
    fireEvent.click(await screen.findByRole("button", { name: "确认导入" }));
    await screen.findByRole("heading", { name: "已确认" });
    expect(client.saveSnapshot).not.toHaveBeenCalled();
    expect(client.getSnapshot).toHaveBeenCalledTimes(1);
    expect(client.getNav).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("可用现金")).toHaveProperty("value", "123.4500");
  });
  it("requires reauthentication after 401 and clears identity-specific drafts and rows", async () => {
    await login();
    fireEvent.change(screen.getByLabelText("可用现金"), { target: { value: "PRIVATE-CASH" } });
    vi.mocked(client.saveSnapshot).mockRejectedValue(
      new client.ApiError(401, "授权已失效，请重新安全连接。"),
    );
    fireEvent.click(screen.getByRole("button", { name: "保存账户快照" }));
    await screen.findByRole("button", { name: "安全连接" });
    expect(screen.queryByText("510500")).toBeNull();
    expect(screen.queryByDisplayValue("PRIVATE-CASH")).toBeNull();
    await screen.findByRole("alert");
    cleanup();
    vi.mocked(oidc.completeAuthorization).mockReturnValue(Promise.resolve({ token: "memory-token", expiresIn: 300 }));
    render(<Workspace />);
    await waitFor(() =>
      expect(screen.getByLabelText("可用现金")).toHaveProperty("value", "123.4500"),
    );
  });
});
describe("current signal stream", () => {
  it("does not subscribe to an empty symbol set", async () => {
    vi.mocked(client.getSignals).mockResolvedValue([]);
    await login();
    expect(client.streamSignals).not.toHaveBeenCalled();
    expect(screen.getAllByText(/当前没有可订阅的标的/).length).toBeGreaterThan(0);
  });
  it("requeries current state instead of rendering directional historical replay", async () => {
    await login();
    const onEvent = vi.mocked(client.streamSignals).mock.calls[0][3];
    await act(async () =>
      onEvent({
        event: "buy",
        id: "123-0",
        data: { ...signal, direction: "HISTORICAL-BUY", stale: false, status: "BUY" },
      }),
    );
    await waitFor(() => expect(client.getSignals).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("HISTORICAL-BUY")).toBeNull();
    expect(
      within(screen.getByRole("region", { name: "当前策略信号" })).getByText("quote stale"),
    ).toBeTruthy();
    await act(async () => onEvent({ event: "reset", data: { reason: "cursor unavailable" } }));
    await screen.findByText(/信号游标已重置/);
    await waitFor(() => expect(client.getSignals).toHaveBeenCalledTimes(3));
  });
  it("reconnects after a normal EOF using Last-Event-ID and clears the cursor after reset", async () => {
    await login();
    vi.useFakeTimers();
    let onEvent = vi.mocked(client.streamSignals).mock.calls[0][3];
    await act(async () => onEvent({ event: "buy", id: "123-0", data: signal }));
    await act(async () => {
      stream.resolve();
    });
    stream = Promise.withResolvers<void>();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(vi.mocked(client.streamSignals).mock.calls[1][4]).toBe("123-0");
    onEvent = vi.mocked(client.streamSignals).mock.calls[1][3];
    await act(async () => onEvent({ event: "reset", data: { reason: "expired" } }));
    await act(async () => {
      stream.resolve();
    });
    stream = Promise.withResolvers<void>();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(vi.mocked(client.streamSignals).mock.calls[2][4]).toBeUndefined();
  });
});
