import type React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ImportPreview } from "@/lib/a500-client";

const { client } = vi.hoisted(() => ({
  client: {
    confirmPreview: vi.fn(),
    getPreview: vi.fn(),
    uploadPreview: vi.fn(),
  },
}));
vi.mock("@/lib/a500-client", async (importOriginal) => ({
  ...(await importOriginal()),
  ...client,
}));
vi.mock("@/lib/session", () => ({
  clearAccessToken: vi.fn(),
  getAccessToken: () => "memory-token",
  subscribeSession: () => () => undefined,
}));
vi.mock("@/lib/oidc", () => ({
  beginAuthorization: vi.fn(),
  configurationReady: true,
}));
vi.mock("@/components/app-shell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <main>{children}</main>,
}));
import { ApiError } from "@/lib/a500-client";
import TradeImportPage from "@/app/data/trades/page";

const preview: ImportPreview = {
  batch_id: "batch-1",
  valid: 1,
  new: 1,
  duplicates: 0,
  errors: 0,
  error_details: [],
  confirmed: null,
};

beforeEach(() => {
  vi.resetAllMocks();
  window.localStorage.clear();
  client.getPreview.mockResolvedValue(preview);
});
afterEach(cleanup);

describe("trade import page", () => {
  it("prevents confirmation on row errors and shows server locations", async () => {
    client.uploadPreview.mockResolvedValue({
      ...preview,
      errors: 1,
      error_details: [{ row: 4, field: "成交编号", reason: "duplicate conflict" }],
    });
    const rendered = render(<TradeImportPage />);
    const file = new File(["csv"], "trades.csv", { type: "text/csv" });
    fireEvent.change(rendered.container.querySelector('input[type="file"]')!, {
      target: { files: [file] },
    });
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: "上传并预览" }) as HTMLButtonElement).disabled,
      ).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "上传并预览" }));
    const errorList = await screen.findByRole("list", { name: "导入逐行错误" });
    expect(errorList.textContent).toContain("第 4 行");
    expect(errorList.textContent).toContain("成交编号");
    expect(errorList.textContent).toContain("duplicate conflict");
    expect(screen.queryByRole("button", { name: "确认导入" })).toBeNull();
    expect(window.localStorage.getItem("a500.import-batch-id")).toBe("batch-1");
  });

  it("requires explicit confirmation, prevents rapid repeat submission, and reports returned IDs", async () => {
    client.uploadPreview.mockResolvedValue(preview);
    let finish:
      | ((
          value: typeof preview & {
            confirmed: {
              confirmed: boolean;
              new: number;
              duplicates: number;
              inserted_ids: number[];
            };
          },
        ) => void)
      | undefined;
    client.confirmPreview.mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const rendered = render(<TradeImportPage />);
    const fileInput = rendered.container.querySelector('input[type="file"]')!;
    fireEvent.change(fileInput, {
      target: { files: [new File(["csv"], "trades.csv", { type: "text/csv" })] },
    });
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: "上传并预览" }) as HTMLButtonElement).disabled,
      ).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "上传并预览" }));
    const confirm = await screen.findByRole("button", { name: "确认导入" });
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "确认导入" }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    expect(client.confirmPreview).not.toHaveBeenCalled();
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    expect(client.confirmPreview).toHaveBeenCalledTimes(1);
    finish?.({
      ...preview,
      confirmed: { confirmed: true, new: 1, duplicates: 0, inserted_ids: [42] },
    });
    expect((await screen.findByText(/新增记录编号：42/)).textContent).toContain("42");
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "已确认" }).textContent).toBe("已确认"),
    );
  });

  it("explains an expired batch and leaves it replaceable", async () => {
    window.localStorage.setItem("a500.import-batch-id", "expired");
    client.getPreview.mockRejectedValue(new ApiError(404, "Preview not found"));
    const rendered = render(<TradeImportPage />);
    expect((await screen.findByRole("alert")).textContent).toContain("批次已过期或不可用");
    expect(window.localStorage.getItem("a500.import-batch-id")).toBeNull();
    expect(rendered.container.querySelector('input[type="file"]')).not.toBeNull();
  });

  it("rejects empty, non-CSV, and oversized files before sending them", async () => {
    const rendered = render(<TradeImportPage />);
    const input = rendered.container.querySelector('input[type="file"]')!;
    for (const file of [
      new File([], "empty.csv", { type: "text/csv" }),
      new File(["data"], "trades.txt", { type: "text/plain" }),
      new File([new Uint8Array(5 * 1024 * 1024 + 1)], "large.csv", { type: "text/csv" }),
    ]) {
      fireEvent.change(input, { target: { files: [file] } });
      expect((await screen.findByRole("alert")).textContent).toMatch(/所选文件|扩展名|超过 5 MiB/);
    }
    expect(client.uploadPreview).not.toHaveBeenCalled();
  });

  it("allows cancelling an upload and explains that no preview was returned", async () => {
    let signal: AbortSignal | undefined;
    client.uploadPreview.mockImplementation(
      (_token: string, _file: File, requestSignal: AbortSignal) => {
        signal = requestSignal;
        return new Promise((_resolve, reject) =>
          requestSignal.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          ),
        );
      },
    );
    const rendered = render(<TradeImportPage />);
    fireEvent.change(rendered.container.querySelector('input[type="file"]')!, {
      target: { files: [new File(["data"], "trades.csv", { type: "text/csv" })] },
    });
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: "上传并预览" }) as HTMLButtonElement).disabled,
      ).toBe(false),
    );
    fireEvent.click(await screen.findByRole("button", { name: "上传并预览" }));
    fireEvent.click(await screen.findByRole("button", { name: "取消上传" }));
    expect(signal?.aborted).toBe(true);
    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toMatch(/已取消上传.*没有收到预览/),
    );
    expect(screen.queryByRole("heading", { name: "检查导入预览" })).toBeNull();
  });

  it("does not show success after a lost confirmation response and offers safe reconciliation", async () => {
    client.uploadPreview.mockResolvedValue(preview);
    client.confirmPreview.mockRejectedValue(new TypeError("Failed to fetch"));
    client.getPreview.mockRejectedValue(new TypeError("Failed to fetch"));
    const rendered = render(<TradeImportPage />);
    fireEvent.change(rendered.container.querySelector('input[type="file"]')!, {
      target: { files: [new File(["data"], "trades.csv", { type: "text/csv" })] },
    });
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: "上传并预览" }) as HTMLButtonElement).disabled,
      ).toBe(false),
    );
    fireEvent.click(await screen.findByRole("button", { name: "上传并预览" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认导入" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/无法确认服务端最终状态/);
    expect(screen.queryByRole("heading", { name: "已确认" })).toBeNull();
    expect(screen.getByRole("button", { name: "重新查询批次状态" })).toBeTruthy();
  });

  it("reconciles a successful confirmation whose response was interrupted", async () => {
    client.uploadPreview.mockResolvedValue(preview);
    client.confirmPreview.mockRejectedValue(new TypeError("Failed to fetch"));
    client.getPreview.mockResolvedValue({
      ...preview,
      confirmed: { confirmed: true, new: 1, duplicates: 0, inserted_ids: [42] },
    });
    const rendered = render(<TradeImportPage />);
    fireEvent.change(rendered.container.querySelector('input[type="file"]')!, {
      target: { files: [new File(["data"], "trades.csv", { type: "text/csv" })] },
    });
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: "上传并预览" }) as HTMLButtonElement).disabled,
      ).toBe(false),
    );
    fireEvent.click(await screen.findByRole("button", { name: "上传并预览" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认导入" }));
    expect(await screen.findByRole("heading", { name: "已确认" })).toBeTruthy();
    expect(client.getPreview).toHaveBeenCalledTimes(1);
  });
  it("requires acknowledging duplicate rows before confirming an import", async () => {
    client.uploadPreview.mockResolvedValue({ ...preview, duplicates: 2, new: 0 });
    const rendered = render(<TradeImportPage />);
    fireEvent.change(rendered.container.querySelector('input[type="file"]')!, {
      target: { files: [new File(["data"], "trades.csv", { type: "text/csv" })] },
    });
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: "上传并预览" }) as HTMLButtonElement).disabled,
      ).toBe(false),
    );
    fireEvent.click(await screen.findByRole("button", { name: "上传并预览" }));
    const confirm = await screen.findByRole("button", { name: "确认导入" });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox", { name: /2 条重复记录不会再次写入/ }));
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
  });
});
