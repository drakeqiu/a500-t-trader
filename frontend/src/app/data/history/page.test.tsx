import type React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DailyHistoryPage, HistoryJob, HistoryJobPage } from "@/lib/a500-client";

const { client } = vi.hoisted(() => ({
  client: {
    listHistoryJobs: vi.fn(),
    createHistoryJob: vi.fn(),
    getHistoryJob: vi.fn(),
    cancelHistoryJob: vi.fn(),
    getDailyHistory: vi.fn(),
    listDailyHistorySymbols: vi.fn(),
    listDailyHistorySymbolRanges: vi.fn(),
    deleteHistoryJob: vi.fn(),
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
import HistoryPage from "@/app/data/history/page";

function jobsPage(jobs: HistoryJob[], total = jobs.length): HistoryJobPage {
  return { jobs, total, limit: 30, offset: 0, has_more: total > jobs.length };
}

const dailyPage: DailyHistoryPage = {
  symbol: "510300",
  start_date: "2026-01-01",
  end_date: "2026-01-03",
  covered: true,
  has_more: false,
  bars: [
    {
      date: "2026-01-02",
      open: "1",
      high: "2",
      low: "1",
      close: "2",
      volume: 100,
      amount: "200",
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
};

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
  client.listHistoryJobs.mockResolvedValue(jobsPage([]));
  client.listDailyHistorySymbols.mockResolvedValue({
    symbols: [{ symbol: "510300", start_date: "2026-01-01", end_date: "2026-01-03" }],
  });
  client.listDailyHistorySymbolRanges.mockResolvedValue({
    symbol: "510300",
    ranges: [
      { start_date: "2026-01-01", end_date: "2026-01-03" },
      { start_date: "2026-01-07", end_date: "2026-01-09" },
    ],
  });
  window.localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("history task page", () => {
  it("loads recent jobs from the account in a browser with no saved job IDs", async () => {
    client.listHistoryJobs.mockResolvedValue(jobsPage([job]));
    render(<HistoryPage />);

    expect(await screen.findByText(job.id)).toBeTruthy();
    expect(client.listHistoryJobs).toHaveBeenCalledWith(
      "memory-token",
      { limit: 30, offset: 0 },
      expect.any(AbortSignal),
    );
    expect(window.localStorage.getItem("a500.history-job-ids")).toBeNull();
  });

  it("does not present an accepted job as completed and uses non-percent status feedback without progress data", async () => {
    client.createHistoryJob.mockResolvedValue(job);
    render(<HistoryPage />);
    fireEvent.change(await screen.findByLabelText("证券代码"), {
      target: { value: "SH.510500" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
    expect(
      await screen.findByText("任务已提交，正在等待后端执行。提交成功不代表行情已拉取完成。"),
    ).toBeTruthy();
    expect(screen.getByText("等待执行")).toBeTruthy();
    expect(screen.getByText("正在等待后端处理；当前没有可用的精确百分比。")).toBeTruthy();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(client.listHistoryJobs).toHaveBeenCalled();
  });

  it("shows progress only when the backend reports valid chunk totals", async () => {
    const running = {
      ...job,
      status: "running" as const,
      stage: "fetching",
      total_chunks: 4,
      completed_chunks: 1,
    };
    client.listHistoryJobs.mockResolvedValue(jobsPage([running]));
    client.getHistoryJob.mockResolvedValue(running);
    render(<HistoryPage />);
    expect(await screen.findByRole("progressbar", { name: "任务进度 25%" })).toBeTruthy();
    expect(screen.getByText("25%")).toBeTruthy();
  });

  it("restores task state after refresh and shows backend failure details", async () => {
    const failed = {
      ...job,
      status: "failed" as const,
      error: "供应商连接超时",
    };
    client.listHistoryJobs.mockResolvedValue(jobsPage([failed]));
    client.getHistoryJob.mockResolvedValue(failed);
    render(<HistoryPage />);
    expect(await screen.findByText("失败")).toBeTruthy();
    expect(screen.getByText("失败原因：供应商连接超时")).toBeTruthy();
    expect(client.listHistoryJobs).toHaveBeenCalledWith(
      "memory-token",
      { limit: 30, offset: 0 },
      expect.any(AbortSignal),
    );
  });

  it("shows terminal progress from confirmed chunks without marking failures or cancellations complete", async () => {
    const terminalJobs = [
      { status: "failed" as const, completed_chunks: 2, total_chunks: 4, percent: 50 },
      { status: "cancelled" as const, completed_chunks: 3, total_chunks: 4, percent: 75 },
    ];
    for (const [index, task] of terminalJobs.entries()) {
      const terminalJob = {
        ...job,
        id: `terminal-${index}`,
        status: task.status,
        completed_chunks: task.completed_chunks,
        total_chunks: task.total_chunks,
      };
      client.listHistoryJobs.mockResolvedValue(jobsPage([terminalJob]));
      client.getHistoryJob.mockResolvedValueOnce(terminalJob);
      const { unmount } = render(<HistoryPage />);
      expect(
        await screen.findByRole("progressbar", {
          name: `任务进度 ${task.percent}%`,
        }),
      ).toBeTruthy();
      expect(screen.getByText(`${task.percent}%`)).toBeTruthy();
      unmount();
    }
  });

  it("does not report exact progress when chunk counts are inconsistent", async () => {
    const inconsistent = {
      ...job,
      status: "running" as const,
      total_chunks: 4,
      completed_chunks: 5,
    };
    client.listHistoryJobs.mockResolvedValue(jobsPage([inconsistent]));
    client.getHistoryJob.mockResolvedValue(inconsistent);
    render(<HistoryPage />);
    expect(await screen.findByText("5 / 4")).toBeTruthy();
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("does not report exact progress for a succeeded job with incomplete chunks", async () => {
    const inconsistent = {
      ...job,
      status: "succeeded" as const,
      total_chunks: 4,
      completed_chunks: 1,
    };
    client.listHistoryJobs.mockResolvedValue(jobsPage([inconsistent]));
    client.getHistoryJob.mockResolvedValue(inconsistent);
    render(<HistoryPage />);
    expect(await screen.findByText("已完成")).toBeTruthy();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.queryByText("25%")).toBeNull();
  });

  it("does not show task completion when a cancellation follows the last checkpoint", async () => {
    const cancelled = {
      ...job,
      status: "cancelled" as const,
      total_chunks: 4,
      completed_chunks: 4,
    };
    client.listHistoryJobs.mockResolvedValue(jobsPage([cancelled]));
    client.getHistoryJob.mockResolvedValue(cancelled);
    render(<HistoryPage />);
    expect(await screen.findByText("4 / 4")).toBeTruthy();
    expect((await screen.findAllByText("已取消")).length).toBe(1);
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.queryByText("100%")).toBeNull();
    expect(screen.queryByText("99%")).toBeNull();
  });

  it("shows succeeded jobs as complete without exposing task-bound bars", async () => {
    const succeeded = {
      ...job,
      status: "succeeded" as const,
      total_chunks: 1,
      completed_chunks: 1,
    };
    client.listHistoryJobs.mockResolvedValue(jobsPage([succeeded]));
    client.getHistoryJob.mockResolvedValue(succeeded);
    render(<HistoryPage />);
    expect((await screen.findAllByText("已完成")).length).toBe(2);
    expect(screen.getByRole("progressbar", { name: "任务进度 100%" })).toBeTruthy();
    expect(screen.getByText("100%")).toBeTruthy();
    expect(screen.getByRole("button", { name: "刷新状态" })).toBeTruthy();
    expect(screen.queryByText("查看结果")).toBeNull();
    expect(screen.queryByText("行情结果")).toBeNull();
    expect(client.listHistoryJobs).toHaveBeenCalled();
  });
  it("shows job-list errors and retries loading recent jobs", async () => {
    client.listHistoryJobs.mockRejectedValueOnce(new Error("网络不可用"));
    render(<HistoryPage />);
    expect(await screen.findByText("网络不可用")).toBeTruthy();

    client.listHistoryJobs.mockResolvedValue(jobsPage([job]));
    fireEvent.click(screen.getByRole("button", { name: "刷新列表" }));
    expect(await screen.findByText(job.id)).toBeTruthy();
  });

  it("keeps the last confirmed task state explicit when an automatic status refresh fails", async () => {
    client.listHistoryJobs.mockResolvedValue(jobsPage([job]));
    client.getHistoryJob.mockRejectedValue(new Error("服务暂不可用"));
    render(<HistoryPage />);
    expect(await screen.findByText("等待执行")).toBeTruthy();
    await waitFor(() => expect(client.getHistoryJob).toHaveBeenCalled(), { timeout: 5000 });
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
    fireEvent.change(await screen.findByLabelText("证券代码"), {
      target: { value: "SH.510500" },
    });
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
    fireEvent.change(await screen.findByLabelText("证券代码"), {
      target: { value: "SH.510500" },
    });
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
    client.listHistoryJobs.mockResolvedValue(jobsPage([running]));
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
  it("lazy-loads exact downloaded coverage when a security is expanded", async () => {
    render(<HistoryPage />);
    expect(await screen.findByRole("heading", { name: "已下载的证券" })).toBeTruthy();
    expect(screen.getByText("510300")).toBeTruthy();
    expect(client.listDailyHistorySymbolRanges).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("510300"));
    expect(await screen.findByText("2026-01-01 至 2026-01-03")).toBeTruthy();
    expect(screen.getByText("2026-01-07 至 2026-01-09")).toBeTruthy();
    expect(client.listDailyHistorySymbolRanges).toHaveBeenCalledWith(
      "memory-token",
      "510300",
      expect.any(AbortSignal),
    );
    expect(client.getDailyHistory).not.toHaveBeenCalled();
  });

  it("asks before deleting terminal jobs and removes only the task record", async () => {
    const succeeded = { ...job, status: "succeeded" as const };
    client.listHistoryJobs.mockResolvedValue(jobsPage([succeeded]));
    const confirmation = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<HistoryPage />);
    fireEvent.click(screen.getByText(/任务记录/, { selector: "summary" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除任务记录" }));

    await waitFor(() =>
      expect(client.deleteHistoryJob).toHaveBeenCalledWith(
        "memory-token",
        succeeded.id,
        expect.any(AbortSignal),
      ),
    );
    expect(confirmation).toHaveBeenCalledWith(expect.stringContaining("共享历史行情不会被删除"));
    await waitFor(() => expect(screen.queryByText(succeeded.id)).toBeNull());
    expect(screen.getByText("任务记录已删除，共享历史行情保留。")).toBeTruthy();
  });
});
