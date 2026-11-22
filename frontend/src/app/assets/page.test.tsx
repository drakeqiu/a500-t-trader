import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as Client from "@/lib/a500-client";
const { getNav, getSnapshot, saveSnapshot } = vi.hoisted(() => ({
  getNav: vi.fn(),
  getSnapshot: vi.fn(),
  saveSnapshot: vi.fn(),
}));
vi.mock("@/lib/a500-client", async (importOriginal) => ({
  ...(await importOriginal<typeof Client>()),
  getNav,
  getSnapshot,
  saveSnapshot,
}));
vi.mock("@/lib/oidc", () => ({ configurationReady: true, beginAuthorization: vi.fn() }));

import { ApiError, type AccountSnapshot, type SnapshotResult } from "@/lib/a500-client";
import { clearAccessToken, setAccessToken } from "@/lib/session";
import AssetsPage from "@/app/assets/page";

const snapshot: AccountSnapshot = {
  as_of: "2026-11-06T15:00:00+08:00",
  available_cash: "125.5000",
  holdings: { "510300": 100 },
  protected_floor: { "510300": 20 },
  sellable: { "510300": 80 },
  cost_basis: { "510300": "3.8500" },
};
const stored: SnapshotResult = { snapshot, etag: '"v1"' };

beforeEach(() => {
  vi.clearAllMocks();
  setAccessToken("memory-token", 300);
  getNav.mockResolvedValue({
    status: "unavailable",
    nav: null,
    reason: "quote_future",
    holdings: [],
  });
  getSnapshot.mockResolvedValue(null);
  saveSnapshot.mockResolvedValue({ snapshot, etag: '"v1"' });
});
afterEach(() => {
  cleanup();
  clearAccessToken();
});
function fillPosition(row = 1) {
  fireEvent.click(screen.getByRole("button", { name: "添加证券" }));
  fireEvent.change(screen.getByLabelText(`第 ${row} 行证券代码`), { target: { value: "510300" } });
  fireEvent.change(screen.getByLabelText(`第 ${row} 行持有总份额`), { target: { value: "100" } });
  fireEvent.change(screen.getByLabelText(`第 ${row} 行保护底仓`), { target: { value: "20" } });
  fireEvent.change(screen.getByLabelText(`第 ${row} 行当前可卖`), { target: { value: "80" } });
  fireEvent.change(screen.getByLabelText(`第 ${row} 行成本基础`), { target: { value: "3.8500" } });
}

describe("assets route", () => {
  it("shows saved positions as a labeled table instead of raw JSON", async () => {
    getSnapshot.mockResolvedValue(stored);
    render(<AssetsPage />);
    expect(await screen.findByText("¥125.5000")).toBeTruthy();
    expect(screen.getByText("净资产估值与账户快照。")).toBeTruthy();
    expect(
      screen.queryByText("净资产按账户现金和持仓行情估算，不包含保护底仓、可卖数量及持仓成本。"),
    ).toBeNull();
    const navHelp = screen.getByRole("note", { name: "净资产估值说明" });
    expect(navHelp.getAttribute("title")).toBeNull();
    expect(navHelp.getAttribute("aria-description")).toBe(
      "净资产按账户现金和持仓行情估算，不包含保护底仓、可卖数量及持仓成本。",
    );
    expect(screen.queryByText("保存后会用当前内容完整替换现有账户快照。")).toBeNull();
    const snapshotHelp = screen.getByRole("note", { name: "账户快照说明" });
    expect(snapshotHelp.getAttribute("title")).toBeNull();
    expect(snapshotHelp.getAttribute("aria-description")).toBe(
      "保存后会用当前内容完整替换现有账户快照。",
    );
    const table = screen.getByRole("table", { name: "证券持仓" });
    expect(table).toBeTruthy();
    expect(screen.getByRole("row", { name: /510300/ })).toBeTruthy();
    for (const label of ["持仓数量", "保护底仓", "可卖数量", "成本基础"]) {
      expect(screen.getByRole("columnheader", { name: label })).toBeTruthy();
    }
    expect(screen.getByText("3.8500")).toBeTruthy();
    expect(screen.queryByText(/\{/)).toBeNull();
    expect(screen.queryByLabelText("可用现金")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "编辑快照" }));
    expect((screen.getByLabelText("可用现金") as HTMLInputElement).value).toBe("125.5000");
    expect(screen.queryByLabelText("快照时间")).toBeNull();
    expect((screen.getByLabelText("第 1 行持有总份额") as HTMLInputElement).value).toBe("100");
    expect(screen.queryByLabelText(/JSON/)).toBeNull();
    expect(screen.getByText(/不打算卖出的保留份额/)).toBeTruthy();
  });

  it("explains when a saved snapshot has no positions", async () => {
    getSnapshot.mockResolvedValue({
      ...stored,
      snapshot: {
        ...snapshot,
        holdings: {},
        protected_floor: {},
        sellable: {},
        cost_basis: {},
      },
    });
    render(<AssetsPage />);
    expect(await screen.findByText("当前快照未记录证券持仓。")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "证券持仓" })).toBeTruthy();
    expect(screen.queryByText("{}")).toBeNull();
  });

  it("asks before discarding a dirty draft and keeps it when the user continues editing", async () => {
    getSnapshot.mockResolvedValue(stored);
    render(<AssetsPage />);
    await screen.findByText("¥125.5000");
    fireEvent.click(screen.getByRole("button", { name: "编辑快照" }));
    fireEvent.change(screen.getByLabelText("可用现金"), { target: { value: "222" } });
    fireEvent.click(screen.getByRole("button", { name: "取消编辑" }));
    expect(screen.getByRole("group", { name: "确认放弃未保存更改" })).toBeTruthy();
    expect((screen.getByLabelText("可用现金") as HTMLInputElement).value).toBe("222");
    fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(screen.queryByRole("group", { name: "确认放弃未保存更改" })).toBeNull();
    expect((screen.getByLabelText("可用现金") as HTMLInputElement).value).toBe("222");
    fireEvent.click(screen.getByRole("button", { name: "取消编辑" }));
    fireEvent.click(screen.getByRole("button", { name: "放弃更改" }));
    expect(screen.getByRole("button", { name: "编辑快照" })).toBeTruthy();
    expect(screen.queryByLabelText("可用现金")).toBeNull();
  });

  it("generates UTC snapshot time when creating without a time field", async () => {
    const saveStartedAt = Date.now();
    render(<AssetsPage />);
    await screen.findByText("尚无账户快照");
    fireEvent.click(screen.getByRole("button", { name: "创建快照" }));
    expect(screen.queryByLabelText("快照时间")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "创建账户快照" }));
    await waitFor(() => expect(saveSnapshot).toHaveBeenCalledTimes(1));
    const payload = saveSnapshot.mock.calls[0][1];
    expect(payload.as_of).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
    expect(Date.parse(payload.as_of)).toBeGreaterThanOrEqual(saveStartedAt);
    expect(Date.parse(payload.as_of)).toBeLessThanOrEqual(Date.now());
  });
  it("generates a new UTC snapshot time when editing", async () => {
    getSnapshot.mockResolvedValue(stored);
    render(<AssetsPage />);
    await screen.findByText("¥125.5000");
    fireEvent.click(screen.getByRole("button", { name: "编辑快照" }));
    const saveStartedAt = Date.now();
    fireEvent.click(screen.getByRole("button", { name: "保存账户快照" }));
    await waitFor(() => expect(saveSnapshot).toHaveBeenCalledTimes(1));
    const payload = saveSnapshot.mock.calls[0][1];
    expect(payload.as_of).not.toBe(snapshot.as_of);
    expect(payload.as_of).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
    expect(Date.parse(payload.as_of)).toBeGreaterThanOrEqual(saveStartedAt);
    expect(Date.parse(payload.as_of)).toBeLessThanOrEqual(Date.now());
  });

  it("preserves user input after save failure, then sends the position fields as one snapshot", async () => {
    saveSnapshot.mockRejectedValueOnce(new Error("服务暂不可用"));
    render(<AssetsPage />);
    await screen.findByText("尚无账户快照");
    fireEvent.click(screen.getByRole("button", { name: "创建快照" }));
    fireEvent.change(screen.getByLabelText("可用现金"), { target: { value: "987.654321" } });
    fillPosition();
    fireEvent.click(screen.getByRole("button", { name: "创建账户快照" }));
    expect((await screen.findByRole("alert")).textContent).toContain("输入内容已保留");
    expect((screen.getByLabelText("可用现金") as HTMLInputElement).value).toBe("987.654321");
    fireEvent.click(screen.getByRole("button", { name: "创建账户快照" }));
    await waitFor(() =>
      expect(saveSnapshot).toHaveBeenLastCalledWith(
        "memory-token",
        expect.objectContaining({ available_cash: "987.654321" }),
        null,
      ),
    );
    expect((await screen.findByText(/账户快照已保存/)).textContent).toContain("账户快照已保存");
    expect(screen.getByText("¥125.5000")).toBeTruthy();
  });
  it("rejects empty and malformed amounts without sending the draft", async () => {
    render(<AssetsPage />);
    await screen.findByText("尚无账户快照");
    fireEvent.click(screen.getByRole("button", { name: "创建快照" }));
    fireEvent.change(screen.getByLabelText("可用现金"), { target: { value: "NaN" } });
    fireEvent.submit(screen.getByRole("button", { name: "创建账户快照" }).closest("form")!);
    expect((await screen.findByRole("alert")).textContent).toContain("可用现金");
    expect(saveSnapshot).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("可用现金"), { target: { value: "" } });
    fireEvent.submit(screen.getByRole("button", { name: "创建账户快照" }).closest("form")!);
    expect((await screen.findByRole("alert")).textContent).toContain("可用现金");
    expect(saveSnapshot).not.toHaveBeenCalled();
  });
  it("blocks duplicate security rows with a clear message", async () => {
    render(<AssetsPage />);
    await screen.findByText("尚无账户快照");
    fireEvent.click(screen.getByRole("button", { name: "创建快照" }));
    fireEvent.change(screen.getByLabelText("可用现金"), { target: { value: "100" } });
    fillPosition(1);
    fillPosition(2);
    fireEvent.click(screen.getByRole("button", { name: "创建账户快照" }));
    expect((await screen.findByRole("alert")).textContent).toContain("证券代码不能重复");
    expect(saveSnapshot).not.toHaveBeenCalled();
  });

  it("allows only one in-flight save even when submit fires repeatedly", async () => {
    let resolveSave!: (result: SnapshotResult) => void;
    saveSnapshot.mockImplementation(
      () => new Promise<SnapshotResult>((resolve) => (resolveSave = resolve)),
    );
    render(<AssetsPage />);
    await screen.findByText("尚无账户快照");
    fireEvent.click(screen.getByRole("button", { name: "创建快照" }));
    const form = screen.getByRole("button", { name: "创建账户快照" }).closest("form");
    expect(form).not.toBeNull();
    fireEvent.submit(form!);
    fireEvent.submit(form!);
    expect(saveSnapshot).toHaveBeenCalledTimes(1);
    resolveSave(stored);
    await screen.findByText(/账户快照已保存/);
  });

  it("retains the draft through an ETag conflict until the user chooses to use the latest version", async () => {
    const latest: SnapshotResult = {
      snapshot: { ...snapshot, available_cash: "456.00" },
      etag: '"v2"',
    };
    getSnapshot.mockResolvedValueOnce(stored).mockResolvedValueOnce(latest);
    saveSnapshot.mockRejectedValueOnce(new ApiError(412, "冲突"));
    render(<AssetsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "编辑快照" }));
    fireEvent.change(screen.getByLabelText("可用现金"), { target: { value: "999.00" } });
    fireEvent.click(screen.getByRole("button", { name: "保存账户快照" }));
    expect((await screen.findByRole("alert")).textContent).toContain("草稿仍保留");
    expect((screen.getByLabelText("可用现金") as HTMLInputElement).value).toBe("999.00");
    fireEvent.click(screen.getByRole("button", { name: "读取最新快照" }));
    expect(
      await screen.findByText("已读取服务端最新快照。当前草稿保持不变；请选择如何处理版本差异。"),
    );
    fireEvent.click(screen.getByRole("button", { name: "保留草稿并使用最新版本" }));
    expect((screen.getByLabelText("可用现金") as HTMLInputElement).value).toBe("999.00");
    fireEvent.click(screen.getByRole("button", { name: "保存账户快照" }));
    await waitFor(() =>
      expect(saveSnapshot).toHaveBeenLastCalledWith(
        "memory-token",
        {
          ...snapshot,
          as_of: expect.any(String),
          available_cash: "999.00",
        },
        '"v2"',
      ),
    );
  });
});
