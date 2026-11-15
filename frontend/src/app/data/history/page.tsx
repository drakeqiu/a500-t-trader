"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  ApiError,
  cancelHistoryJob,
  createHistoryJob,
  deleteHistoryJob,
  getDailyHistory,
  getHistoryJob,
  listHistoryJobs,
  type DailyHistoryPage,
  type HistoryJob,
} from "@/lib/a500-client";
import { AppShell } from "@/components/app-shell";
import { PageHeading } from "@/components/page-heading";
import { beginAuthorization, configurationReady } from "@/lib/oidc";
import { getAccessToken, subscribeSession } from "@/lib/session";

const terminal = new Set(["succeeded", "failed", "cancelled"]);
const statusLabels: Record<HistoryJob["status"], string> = {
  queued: "等待执行",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};
const stageLabels: Record<string, string> = {
  queued: "等待工作进程领取",
  fetching: "正在获取行情",
  storing: "正在保存结果",
  finished: "处理结束",
};
const shanghaiToday = () =>
  new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(new Date());
const dateDaysAgo = (days: number, today: string) => {
  const date = new Date(`${today}T00:00:00+08:00`);
  date.setDate(date.getDate() - days);
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(date);
};
const abortError = (error: unknown) => error instanceof DOMException && error.name === "AbortError";
const requestSignal = (signal: AbortSignal) =>
  AbortSignal.any([signal, AbortSignal.timeout(15_000)]);
function messageFor(error: unknown) {
  if (error instanceof DOMException && error.name === "TimeoutError")
    return "查询超时，尚未取得新的后端状态；可手动重试。";
  if (error instanceof ApiError && error.status === 401) return "授权已失效，请重新安全连接。";
  if (error instanceof ApiError && error.status === 404) return "任务不存在或不属于当前登录账户。";
  if (error instanceof ApiError && error.status === 409)
    return "任务已进入不可取消阶段；请刷新状态确认结果。";
  if (error instanceof ApiError && error.status === 422) return `请求未被接受：${error.message}`;
  if (error instanceof ApiError && error.status === 503)
    return "历史行情持久化尚未获部署者授权，当前不能创建任务。";
  return error instanceof Error ? error.message : "服务暂时无法处理请求，请稍后重试。";
}
function requestMessageFor(error: unknown, action: "create" | "cancel" | "delete") {
  if (error instanceof DOMException && error.name === "TimeoutError") {
    if (action === "create")
      return "提交响应超时，后端可能已创建任务，但未返回任务编号；请勿盲目重复提交。";
    if (action === "delete") return "删除请求超时，无法确认任务记录是否已删除；请刷新列表确认。";
    return "取消请求超时，无法确认后端是否已收到；请刷新任务状态后再决定是否重试。";
  }
  if (action === "delete" && error instanceof ApiError && error.status === 409)
    return "任务仍在处理中，不能删除；请刷新状态后重试。";
  return messageFor(error);
}
function HistoryPage() {
  const token = useSyncExternalStore(subscribeSession, getAccessToken, () => null);
  const [hydrated, setHydrated] = useState(false);
  const [jobs, setJobs] = useState<HistoryJob[]>([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [jobsHasMore, setJobsHasMore] = useState(false);
  const [loadingJobs, setLoadingJobs] = useState(false);
  const [loadingMoreJobs, setLoadingMoreJobs] = useState(false);
  const [symbol, setSymbol] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [querySymbol, setQuerySymbol] = useState("");
  const [queryStartDate, setQueryStartDate] = useState("");
  const [queryEndDate, setQueryEndDate] = useState("");
  const [queryPageSize, setQueryPageSize] = useState(20);
  const [historyPage, setHistoryPage] = useState<DailyHistoryPage | null>(null);
  const [queryLoading, setQueryLoading] = useState(false);
  const [queryError, setQueryError] = useState("");
  const [today, setToday] = useState("");
  const [now, setNow] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [jobErrors, setJobErrors] = useState<Record<string, string>>({});
  const request = useRef<AbortController | null>(null);
  const queryRequest = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  useEffect(() => {
    const currentDate = shanghaiToday();
    setToday(currentDate);
    setStartDate(dateDaysAgo(365, currentDate));
    setEndDate(dateDaysAgo(1, currentDate));
    setQueryStartDate(dateDaysAgo(365, currentDate));
    setQueryEndDate(dateDaysAgo(1, currentDate));
    setNow(Date.now());
    setHydrated(true);
  }, []);
  useEffect(
    () => () => {
      request.current?.abort();
      queryRequest.current?.abort();
    },
    [],
  );
  const reload = useCallback(
    async (jobId: string, signal: AbortSignal) => {
      if (!token) throw new Error("尚未连接账户。");
      return getHistoryJob(token, jobId, requestSignal(signal));
    },
    [token],
  );
  const connect = useCallback(() => {
    if (!configurationReady) {
      setError("身份服务尚未配置，请联系部署管理员。");
      return;
    }
    sessionStorage.setItem(
      "a500.auth.return",
      `${window.location.pathname}${window.location.search}`,
    );
    void beginAuthorization()
      .then((url) => window.location.assign(url))
      .catch((reason: unknown) => {
        sessionStorage.removeItem("a500.auth.return");
        setError(reason instanceof Error ? reason.message : "无法启动安全连接。");
      });
  }, []);
  const loadJobs = useCallback(
    async (signal: AbortSignal, offset = 0, append = false) => {
      if (!token) return;
      const page = await listHistoryJobs(token, { limit: 30, offset }, requestSignal(signal));
      setJobs((current) =>
        append
          ? [...current, ...page.jobs.filter((job) => !current.some((item) => item.id === job.id))]
          : page.jobs,
      );
      setJobsTotal(page.total);
      setJobsHasMore(page.has_more);
    },
    [token],
  );
  useEffect(() => {
    if (!hydrated || !token) return;
    const controller = new AbortController();
    request.current?.abort();
    request.current = controller;
    setLoadingJobs(true);
    void loadJobs(controller.signal)
      .catch((reason: unknown) => {
        if (!abortError(reason)) setError(messageFor(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingJobs(false);
      });
    return () => controller.abort();
  }, [hydrated, loadJobs, token]);
  useEffect(() => {
    const nextStaleAt = jobs
      .filter((job) => !terminal.has(job.status))
      .map((job) => Date.parse(job.updated_at) + 60_001)
      .filter((deadline) => Number.isFinite(deadline) && deadline > now)
      .sort((left, right) => left - right)[0];
    if (nextStaleAt === undefined) return;
    const timer = setTimeout(() => setNow(Date.now()), nextStaleAt - now);
    return () => clearTimeout(timer);
  }, [jobs, now]);

  useEffect(() => {
    if (!token || !jobs.some((job) => !terminal.has(job.status))) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const active = jobs.filter((job) => !terminal.has(job.status));
      const fresh = await Promise.all(
        active.map(async (job) => {
          try {
            const updated = await reload(job.id, controller.signal);
            setJobErrors((current) => {
              if (!(job.id in current)) return current;
              const next = { ...current };
              delete next[job.id];
              return next;
            });
            return updated;
          } catch (reason) {
            if (!abortError(reason)) {
              setJobErrors((current) => ({ ...current, [job.id]: messageFor(reason) }));
            }
            return undefined;
          }
        }),
      );
      if (controller.signal.aborted) return;
      setJobs((current) => {
        const latest = new Map(
          fresh.filter((job): job is HistoryJob => Boolean(job)).map((job) => [job.id, job]),
        );
        let changed = false;
        const next = current.map((job) => {
          const updated = latest.get(job.id);
          if (!updated) return job;
          const same =
            updated.updated_at === job.updated_at &&
            updated.status === job.status &&
            updated.stage === job.stage &&
            updated.completed_chunks === job.completed_chunks &&
            updated.cancel_requested === job.cancel_requested &&
            updated.error === job.error;
          if (same) return job;
          changed = true;
          return updated;
        });
        return changed ? next : current;
      });
      timer = setTimeout(() => void poll(), 2500);
    };
    timer = setTimeout(() => void poll(), 2500);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [jobs, reload, token]);

  const runRequest = async (action: (signal: AbortSignal) => Promise<void>) => {
    if (inFlight.current) return;
    inFlight.current = true;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    try {
      await action(controller.signal);
    } catch (reason) {
      if (!abortError(reason)) setError(messageFor(reason));
    } finally {
      inFlight.current = false;
      if (request.current === controller) setBusy(false);
    }
  };

  const submit = () =>
    runRequest(async (signal) => {
      if (!token) return;
      try {
        const job = await createHistoryJob(
          token,
          {
            symbol: symbol.trim(),
            start_date: startDate,
            end_date: endDate,
          },
          requestSignal(signal),
        );
        setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)].slice(0, 30));
        setJobsTotal((current) => current + 1);
        setJobsHasMore((current) => current || jobsTotal >= 30);
        setJobErrors((current) => {
          const next = { ...current };
          delete next[job.id];
          return next;
        });
        setNotice("任务已提交，正在等待后端执行。提交成功不代表行情已拉取完成。");
        setSymbol("");
      } catch (reason) {
        throw new Error(requestMessageFor(reason, "create"));
      }
    });

  const cancel = (job: HistoryJob) =>
    runRequest(async (signal) => {
      if (!token) return;
      try {
        const updated = await cancelHistoryJob(token, job.id, requestSignal(signal));
        setJobs((current) => current.map((item) => (item.id === updated.id ? updated : item)));
        setNotice(
          updated.status === "cancelled"
            ? "任务已取消。"
            : "已请求取消；后端仍在处理中，状态会继续更新。",
        );
      } catch (reason) {
        throw new Error(requestMessageFor(reason, "cancel"));
      }
    });

  const refreshTask = (jobId: string) =>
    runRequest(async (signal) => {
      try {
        const updated = await reload(jobId, signal);
        setJobs((current) => current.map((job) => (job.id === updated.id ? updated : job)));
        setJobErrors((current) => {
          const next = { ...current };
          delete next[jobId];
          return next;
        });
      } catch (reason) {
        if (!abortError(reason))
          setJobErrors((current) => ({ ...current, [jobId]: messageFor(reason) }));
      }
    });

  const refreshJobs = () =>
    runRequest(async (signal) => {
      await loadJobs(signal);
      setError("");
    });

  const loadMoreJobs = () =>
    runRequest(async (signal) => {
      setLoadingMoreJobs(true);
      try {
        await loadJobs(signal, jobs.length, true);
      } finally {
        setLoadingMoreJobs(false);
      }
    });

  const queryHistory = async (offset = 0, pageSize = queryPageSize) => {
    if (!token) return;
    queryRequest.current?.abort();
    const controller = new AbortController();
    queryRequest.current = controller;
    setQueryLoading(true);
    setQueryError("");
    try {
      const page = await getDailyHistory(
        token,
        {
          symbol: querySymbol.trim(),
          start_date: queryStartDate,
          end_date: queryEndDate,
          limit: pageSize,
          offset,
        },
        requestSignal(controller.signal),
      );
      setHistoryPage(page);
    } catch (reason) {
      if (!abortError(reason)) {
        setHistoryPage(null);
        setQueryError(messageFor(reason));
      }
    } finally {
      if (queryRequest.current === controller) {
        queryRequest.current = null;
        setQueryLoading(false);
      }
    }
  };

  const remove = (job: HistoryJob) => {
    if (!window.confirm(`确定删除任务 ${job.id} 的记录吗？共享历史行情不会被删除。`)) return;
    runRequest(async (signal) => {
      if (!token) return;
      try {
        await deleteHistoryJob(token, job.id, requestSignal(signal));
        setJobs((current) => current.filter((item) => item.id !== job.id));
        setJobsTotal((current) => Math.max(0, current - 1));
        setJobErrors((current) => {
          const next = { ...current };
          delete next[job.id];
          return next;
        });
        setNotice("任务记录已删除，共享历史行情保留。");
      } catch (reason) {
        throw new Error(requestMessageFor(reason, "delete"));
      }
    });
  };

  const canSubmit = Boolean(
    token &&
    symbol.trim() &&
    startDate &&
    endDate &&
    startDate <= endDate &&
    endDate < today &&
    !busy,
  );
  return (
    <AppShell activePath="/data/history" connected={Boolean(token)} onConnect={connect}>
      <div className="history-page">
        <PageHeading
          as="header"
          className="history-heading"
          title="历史行情"
          description="按标的与日期范围创建日线拉取任务；任务按登录账户隔离。"
        >
          <span className="history-date-note">日期以北京时间为准</span>
        </PageHeading>
        {!token ? (
          <section className="history-panel" aria-labelledby="history-auth-title">
            <h2 id="history-auth-title">连接账户后管理历史任务</h2>
            <p>任务与行情结果仅对提交任务的登录主体可见。</p>
            <button
              className="trade-import-primary"
              disabled={!hydrated || !configurationReady}
              onClick={connect}
            >
              安全连接
            </button>
          </section>
        ) : (
          <>
            <section className="history-panel history-query" aria-labelledby="history-query-title">
              <div className="history-section-heading">
                <div>
                  <h2 id="history-query-title">查询共享日线</h2>
                  <p>按标的和日期范围读取已保存的行情；查询不会创建任务或触发下载。</p>
                </div>
              </div>
              <div className="history-form">
                <label>
                  查询证券代码
                  <input
                    autoComplete="off"
                    placeholder="例如 510500 或 SH.510500"
                    value={querySymbol}
                    onChange={(event) => setQuerySymbol(event.target.value)}
                  />
                </label>
                <label>
                  开始日期
                  <input
                    type="date"
                    value={queryStartDate}
                    onChange={(event) => setQueryStartDate(event.target.value)}
                  />
                </label>
                <label>
                  结束日期
                  <input
                    type="date"
                    value={queryEndDate}
                    onChange={(event) => setQueryEndDate(event.target.value)}
                  />
                </label>
                <button
                  className="trade-import-primary"
                  disabled={
                    !hydrated ||
                    !querySymbol.trim() ||
                    !queryStartDate ||
                    !queryEndDate ||
                    queryStartDate > queryEndDate ||
                    queryLoading
                  }
                  onClick={() => void queryHistory()}
                >
                  {queryLoading ? "正在查询…" : "查询行情"}
                </button>
              </div>
              {queryError && (
                <p className="history-error" role="alert">
                  {queryError}
                </p>
              )}
              {historyPage && (
                <div className="history-results" aria-live="polite">
                  {!historyPage.covered ? (
                    <p className="history-muted">
                      该日期范围尚未完整覆盖。当前只读取已有数据，不会自动补拉行情。
                    </p>
                  ) : historyPage.total === 0 ? (
                    <p className="history-muted">
                      日期范围已完整覆盖，但没有已观察到的交易日数据。
                    </p>
                  ) : (
                    <>
                      <div className="history-query-summary">
                        <p>
                          {historyPage.symbol} · {historyPage.start_date} 至 {historyPage.end_date}
                        </p>
                        <label>
                          每页行数
                          <select
                            value={queryPageSize}
                            onChange={(event) => {
                              const nextSize = Number(event.target.value);
                              setQueryPageSize(nextSize);
                              void queryHistory(0, nextSize);
                            }}
                          >
                            <option value={10}>10</option>
                            <option value={20}>20</option>
                            <option value={50}>50</option>
                          </select>
                        </label>
                      </div>
                      <div className="history-table-wrap">
                        <table className="history-bars-table">
                          <thead>
                            <tr>
                              <th scope="col">日期</th>
                              <th scope="col">开盘</th>
                              <th scope="col">最高</th>
                              <th scope="col">最低</th>
                              <th scope="col">收盘</th>
                              <th scope="col">成交量</th>
                              {historyPage.bars.some((bar) => bar.amount !== null) && (
                                <th scope="col">成交额</th>
                              )}
                            </tr>
                          </thead>
                          <tbody>
                            {historyPage.bars.map((bar) => (
                              <tr key={bar.date}>
                                <th scope="row">{bar.date}</th>
                                <td>{bar.open}</td>
                                <td>{bar.high}</td>
                                <td>{bar.low}</td>
                                <td>{bar.close}</td>
                                <td>{bar.volume.toLocaleString("zh-CN")}</td>
                                {historyPage.bars.some((item) => item.amount !== null) && (
                                  <td>{bar.amount ?? "—"}</td>
                                )}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                      <div className="history-pagination">
                        <span>
                          共 {historyPage.total} 条 · 第 {historyPage.offset + 1}–
                          {historyPage.offset + historyPage.bars.length} 条
                        </span>
                        <div>
                          <button
                            className="trade-import-secondary"
                            disabled={queryLoading || historyPage.offset === 0}
                            onClick={() =>
                              void queryHistory(Math.max(0, historyPage.offset - queryPageSize))
                            }
                          >
                            上一页
                          </button>
                          <button
                            className="trade-import-secondary"
                            disabled={queryLoading || !historyPage.has_more}
                            onClick={() => void queryHistory(historyPage.offset + queryPageSize)}
                          >
                            下一页
                          </button>
                        </div>
                      </div>
                    </>
                  )}
                </div>
              )}
            </section>
            <section
              className="history-panel history-create"
              aria-labelledby="history-create-title"
            >
              <div className="history-section-heading">
                <div>
                  <h2 id="history-create-title">创建拉取任务</h2>
                  <p>仅支持历史日线；结束日期不能是今天或未来日期。</p>
                </div>
              </div>
              <div className="history-form">
                <label>
                  证券代码
                  <input
                    autoComplete="off"
                    placeholder="例如 510500 或 SH.510500"
                    value={symbol}
                    onChange={(event) => setSymbol(event.target.value)}
                  />
                </label>
                <label>
                  开始日期
                  <input
                    type="date"
                    value={startDate}
                    max={today ? endDate || dateDaysAgo(1, today) : undefined}
                    onChange={(event) => setStartDate(event.target.value)}
                  />
                </label>
                <label>
                  结束日期
                  <input
                    type="date"
                    value={endDate}
                    min={startDate}
                    max={today ? dateDaysAgo(1, today) : undefined}
                    onChange={(event) => setEndDate(event.target.value)}
                  />
                </label>
                <button
                  className="trade-import-primary"
                  disabled={!hydrated || !canSubmit}
                  onClick={submit}
                >
                  {busy ? "正在提交…" : "创建任务"}
                </button>
              </div>
              <p className="history-form-note">
                任务成功后，共享历史行情会独立提供给其他行情页面；此处仅跟踪任务状态与进度。
              </p>
            </section>
            {error && (
              <p className="history-error" role="alert">
                {error}
              </p>
            )}
            {notice && (
              <p className="history-notice" role="status">
                {notice}
              </p>
            )}
            <details className="history-jobs">
              <summary>
                任务记录 · 已加载 {jobs.length} / {jobsTotal} 项
              </summary>
              <section className="history-job-content" aria-labelledby="history-jobs-title">
                <div className="history-section-heading">
                  <div>
                    <h2 id="history-jobs-title">任务状态与进度</h2>
                    <p>任务按账户保存；刷新或加载更多可查看全部历史记录。</p>
                  </div>
                  <div className="history-job-actions">
                    <span>
                      {jobs.length} / {jobsTotal} 项
                    </span>
                    <button
                      className="trade-import-secondary"
                      disabled={busy || loadingJobs}
                      onClick={refreshJobs}
                    >
                      刷新列表
                    </button>
                  </div>
                </div>
                {jobs.length === 0 ? (
                  <div className="history-empty">
                    <h3>{loadingJobs ? "正在加载任务记录" : "暂无历史任务"}</h3>
                    <p>提交任务后，状态和结果会显示在此处。</p>
                  </div>
                ) : (
                  <div className="history-job-list">
                    {jobs.map((job) => {
                      const hasProgress =
                        Number.isInteger(job.total_chunks) &&
                        job.total_chunks > 0 &&
                        Number.isInteger(job.completed_chunks) &&
                        job.completed_chunks >= 0 &&
                        job.completed_chunks <= job.total_chunks &&
                        (job.status !== "succeeded" || job.completed_chunks === job.total_chunks) &&
                        !(
                          terminal.has(job.status) &&
                          job.status !== "succeeded" &&
                          job.completed_chunks === job.total_chunks
                        );
                      const progress = hasProgress
                        ? job.status === "succeeded" && job.completed_chunks === job.total_chunks
                          ? 100
                          : Math.min(
                              99,
                              Math.floor((job.completed_chunks / job.total_chunks) * 100),
                            )
                        : null;
                      return (
                        <article className="history-job" key={job.id}>
                          <div className="history-job-head">
                            <span className={`history-status is-${job.status}`}>
                              {statusLabels[job.status]}
                            </span>
                          </div>
                          <dl className="history-job-meta">
                            <div>
                              <dt>任务编号</dt>
                              <dd>
                                <code>{job.id}</code>
                              </dd>
                            </div>
                            <div>
                              <dt>阶段</dt>
                              <dd>{stageLabels[job.stage] ?? job.stage}</dd>
                            </div>
                            <div>
                              <dt>分片</dt>
                              <dd>
                                {job.completed_chunks} / {job.total_chunks}
                              </dd>
                            </div>
                          </dl>
                          <div className="history-progress" aria-live="polite">
                            {progress !== null &&
                            (job.status === "running" || terminal.has(job.status)) ? (
                              <>
                                <div className="history-progress-label">
                                  <span>
                                    {job.status === "running"
                                      ? (stageLabels[job.stage] ?? "任务正在处理")
                                      : statusLabels[job.status]}
                                  </span>
                                  <span>{progress}%</span>
                                </div>
                                <progress
                                  max={100}
                                  value={progress}
                                  aria-label={`任务进度 ${progress}%`}
                                />
                              </>
                            ) : (
                              !terminal.has(job.status) && (
                                <p>
                                  {job.cancel_requested
                                    ? "取消请求已送达，等待后端停止当前工作。"
                                    : "正在等待后端处理；当前没有可用的精确百分比。"}
                                </p>
                              )
                            )}
                          </div>
                          {jobErrors[job.id] && (
                            <div className="history-error" role="alert">
                              <p>状态查询失败：{jobErrors[job.id]}</p>
                              <p>以下为最近一次成功读取的后端状态，并非最新状态。</p>
                              <button
                                className="trade-import-secondary"
                                disabled={busy}
                                onClick={() => refreshTask(job.id)}
                              >
                                刷新状态
                              </button>
                            </div>
                          )}
                          {job.status === "failed" && (
                            <p className="history-error" role="alert">
                              失败原因：{job.error || "后端未提供失败详情。"}
                            </p>
                          )}
                          {job.status === "cancelled" && (
                            <p className="history-muted">任务已取消，未读取结果。</p>
                          )}
                          {!terminal.has(job.status) &&
                            job.updated_at &&
                            now - Date.parse(job.updated_at) > 60_000 && (
                              <p className="history-muted">
                                后端状态超过 1 分钟未更新；仍在查询，不代表任务失败。
                              </p>
                            )}
                          <div className="history-job-actions">
                            {!terminal.has(job.status) && (
                              <button
                                className="trade-import-secondary"
                                disabled={busy}
                                onClick={() => cancel(job)}
                              >
                                {job.cancel_requested ? "正在取消…" : "请求取消"}
                              </button>
                            )}
                            {terminal.has(job.status) && (
                              <button
                                className="history-remove"
                                disabled={busy}
                                onClick={() => remove(job)}
                              >
                                删除任务记录
                              </button>
                            )}
                            <button
                              className="trade-import-secondary"
                              disabled={busy}
                              onClick={() => refreshTask(job.id)}
                            >
                              刷新状态
                            </button>
                          </div>
                        </article>
                      );
                    })}
                  </div>
                )}
                {jobsHasMore && (
                  <button
                    className="trade-import-secondary history-load-more"
                    disabled={busy || loadingMoreJobs}
                    onClick={loadMoreJobs}
                  >
                    {loadingMoreJobs ? "正在加载…" : "加载更多任务"}
                  </button>
                )}
              </section>
            </details>
          </>
        )}
      </div>
    </AppShell>
  );
}
export default function HistoryRoute() {
  return <HistoryPage />;
}
