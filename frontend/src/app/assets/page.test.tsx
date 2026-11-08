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
    reason: "quote_stale",
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
  it("shows saved snapshot read-only, then explains structured position fields", async () => {
    getSnapshot.mockResolvedValue(stored);
    render(<AssetsPage />);
    expect(await screen.findByText("¥125.5000")).toBeTruthy();
    expect(screen.queryByLabelText("可用现金")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "编辑快照" }));
    expect((screen.getByLabelText("可用现金") as HTMLInputElement).value).toBe("125.5000");
    expect((screen.getByLabelText("快照时间") as HTMLInputElement).value).toBe(snapshot.as_of);
    expect((screen.getByLabelText("第 1 行持有总份额") as HTMLInputElement).value).toBe("100");
    expect(screen.queryByLabelText(/JSON/)).toBeNull();
    expect(screen.getByText(/不打算卖出的保留份额/)).toBeTruthy();
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

  it("preserves user input after save failure, then sends the position fields as one snapshot", async () => {
    saveSnapshot.mockRejectedValueOnce(new Error("服务暂不可用"));
    render(<AssetsPage />);
    await screen.findByText("尚无账户快照");
    fireEvent.click(screen.getByRole("button", { name: "创建快照" }));
    fireEvent.change(screen.getByLabelText("快照时间"), { target: { value: snapshot.as_of } });
    fireEvent.change(screen.getByLabelText("可用现金"), { target: { value: "987.654321" } });
    fillPosition();
    fireEvent.click(screen.getByRole("button", { name: "创建账户快照" }));
    expect((await screen.findByRole("alert")).textContent).toContain("输入内容已保留");
    expect((screen.getByLabelText("可用现金") as HTMLInputElement).value).toBe("987.654321");
    fireEvent.click(screen.getByRole("button", { name: "创建账户快照" }));
    await waitFor(() =>
      expect(saveSnapshot).toHaveBeenCalledWith(
        "memory-token",
        { ...snapshot, available_cash: "987.654321" },
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
    fireEvent.change(screen.getByLabelText("快照时间"), { target: { value: snapshot.as_of } });
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
    fireEvent.change(screen.getByLabelText("快照时间"), { target: { value: snapshot.as_of } });
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
    fireEvent.change(screen.getByLabelText("快照时间"), { target: { value: snapshot.as_of } });
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
        { ...snapshot, available_cash: "999.00" },
        '"v2"',
      ),
    );
  });
});
