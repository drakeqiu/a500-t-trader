import type React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { HistoryJob } from "@/lib/a500-client";

const { client } = vi.hoisted(() => ({
  client: {
    createHistoryJob: vi.fn(),
    getHistoryJob: vi.fn(),
    cancelHistoryJob: vi.fn(),
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
vi.mock("@/lib/oidc", () => ({ beginAuthorization: vi.fn(), configurationReady: true }));
vi.mock("@/components/app-shell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <main>{children}</main>,
}));
import HistoryPage from "@/app/data/history/page";

const job: HistoryJob = {
  type: "daily_history",
  id: "job-1",
  status: "queued",
  stage: "queued",
  cancel_requested: false,
  total_chunks: 0,
  completed_chunks: 0,
  created_at: "2025-01-03T00:00:00+00:00",
  updated_at: "2025-01-03T00:00:00+00:00",
  finished_at: null,
  error: null,
};
beforeEach(() => {
  vi.resetAllMocks();
  window.localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("history task page", () => {
  it("does not present an accepted job as completed and uses non-percent status feedback without progress data", async () => {
    client.createHistoryJob.mockResolvedValue(job);
    render(<HistoryPage />);
    fireEvent.change(await screen.findByLabelText("证券代码"), { target: { value: "SH.510500" } });
    fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
    expect(
      await screen.findByText("任务已提交，正在等待后端执行。提交成功不代表行情已拉取完成。"),
    ).toBeTruthy();
    expect(screen.getByText("等待执行")).toBeTruthy();
    expect(screen.getByText("正在等待后端处理；当前没有可用的精确百分比。")).toBeTruthy();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(window.localStorage.getItem("a500.history-job-ids")).toContain("job-1");
  });

  it("shows progress only when the backend reports valid chunk totals", async () => {
    const running = {
      ...job,
      status: "running" as const,
      stage: "fetching",
      total_chunks: 4,
      completed_chunks: 1,
    };
    window.localStorage.setItem("a500.history-job-ids", JSON.stringify([job.id]));
    client.getHistoryJob.mockResolvedValue(running);
    render(<HistoryPage />);
    expect(await screen.findByRole("progressbar", { name: "任务进度 25%" })).toBeTruthy();
    expect(screen.getByText("25%")).toBeTruthy();
  });

  it("restores task state after refresh and shows backend failure details", async () => {
    const failed = { ...job, status: "failed" as const, error: "供应商连接超时" };
    window.localStorage.setItem("a500.history-job-ids", JSON.stringify([job.id]));
    client.getHistoryJob.mockResolvedValue(failed);
    render(<HistoryPage />);
    expect(await screen.findByText("失败")).toBeTruthy();
    expect(screen.getByText("失败原因：供应商连接超时")).toBeTruthy();
    expect(client.getHistoryJob).toHaveBeenCalledWith(
      "memory-token",
      job.id,
      expect.any(AbortSignal),
    );
  });


  it("keeps succeeded jobs in the task list without fetching or showing task-bound bars", async () => {
    const succeeded = {
      ...job,
      status: "succeeded" as const,
      total_chunks: 1,
      completed_chunks: 1,
    };
    window.localStorage.setItem("a500.history-job-ids", JSON.stringify([job.id]));
    client.getHistoryJob.mockResolvedValue(succeeded);
    render(<HistoryPage />);
    expect(await screen.findByText("已完成")).toBeTruthy();
    expect(screen.getByRole("button", { name: "刷新状态" })).toBeTruthy();
    expect(screen.queryByText("查看结果")).toBeNull();
    expect(screen.queryByText("行情结果")).toBeNull();
    expect(client.getHistoryJob).toHaveBeenCalled();
  });
  it("keeps failed status lookups visible and allows unresolved task IDs to be retried", async () => {
    window.localStorage.setItem("a500.history-job-ids", JSON.stringify([job.id, "job-2"]));
    client.getHistoryJob.mockImplementation((_token: string, id: string) =>
      id === job.id ? Promise.resolve(job) : Promise.reject(new Error("网络不可用")),
    );
    render(<HistoryPage />);
    expect(await screen.findByText("无法确认任务状态")).toBeTruthy();
    expect(screen.getByText(/网络不可用/)).toBeTruthy();
    expect(screen.queryByText("尚无已保存的任务")).toBeNull();
    client.getHistoryJob.mockResolvedValue({ ...job, id: "job-2" });
    fireEvent.click(screen.getByRole("button", { name: "重新查询" }));
    expect(await screen.findByText("SH.510500")).toBeTruthy();
    expect(screen.queryByText("无法确认任务状态")).toBeNull();
  });

  it("keeps the last confirmed task state explicit when a manual refresh fails", async () => {
    window.localStorage.setItem("a500.history-job-ids", JSON.stringify([job.id]));
    client.getHistoryJob
      .mockResolvedValueOnce(job)
      .mockRejectedValueOnce(new Error("服务暂不可用"));
    render(<HistoryPage />);
    expect(await screen.findByText("等待执行")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    expect(await screen.findByText("状态查询失败：服务暂不可用")).toBeTruthy();
    expect(screen.getByText("以下为最近一次成功读取的后端状态，并非最新状态。")).toBeTruthy();
    expect(screen.getByText("等待执行")).toBeTruthy();
  });


  it("does not send duplicate create requests from rapid repeated clicks", async () => {
    let finish: (value: HistoryJob) => void = () => undefined;
    client.createHistoryJob.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    render(<HistoryPage />);
    fireEvent.change(await screen.findByLabelText("证券代码"), { target: { value: "SH.510500" } });
    const submit = screen.getByRole("button", { name: "创建任务" });
    fireEvent.click(submit);
    fireEvent.click(submit);
    expect(client.createHistoryJob).toHaveBeenCalledTimes(1);
    finish(job);
    expect(await screen.findByText("等待执行")).toBeTruthy();
  });
  it("warns that a timed-out create may already exist and blocks blind resubmission", async () => {
    client.createHistoryJob.mockRejectedValue(new DOMException("timed out", "TimeoutError"));
    render(<HistoryPage />);
    fireEvent.change(await screen.findByLabelText("证券代码"), { target: { value: "SH.510500" } });
    fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
    expect(await screen.findByText(/后端可能已创建任务，但未返回任务编号/)).toBeTruthy();
    expect(
      screen.queryByText("任务已提交，正在等待后端执行。提交成功不代表行情已拉取完成。"),
    ).toBeNull();
    expect(client.createHistoryJob).toHaveBeenCalledTimes(1);
  });
  it("warns when an active task has had no backend update for a minute", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2025-01-03T00:00:00Z"));
    const running = {
      ...job,
      status: "running" as const,
      stage: "fetching",
      total_chunks: 10,
      completed_chunks: 3,
    };
    window.localStorage.setItem("a500.history-job-ids", JSON.stringify([job.id]));
    client.getHistoryJob.mockResolvedValue(running);
    render(<HistoryPage />);
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText("30%")).toBeTruthy();
    expect(screen.queryByText("后端状态超过 1 分钟未更新；仍在查询，不代表任务失败。")).toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_001);
    });
    expect(screen.getByText("后端状态超过 1 分钟未更新；仍在查询，不代表任务失败。")).toBeTruthy();
  });
});
