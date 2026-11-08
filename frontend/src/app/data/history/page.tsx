"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  ApiError,
  cancelHistoryJob,
  createHistoryJob,
  getHistoryJob,
  getHistoryResults,
  type HistoryJob,
  type HistoryResults,
} from "@/lib/a500-client";
import { AppShell } from "@/components/app-shell";
import { PageHeading } from "@/components/page-heading";
import { beginAuthorization, configurationReady } from "@/lib/oidc";
import { getAccessToken, subscribeSession } from "@/lib/session";

const storageKey = "a500.history-job-ids";
const pageSize = 50;
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
function requestMessageFor(error: unknown, action: "create" | "cancel") {
  if (error instanceof DOMException && error.name === "TimeoutError") {
    return action === "create"
      ? "提交响应超时，后端可能已创建任务，但未返回任务编号；当前接口无法查询未返回编号的任务。请勿重复提交，先确认后端是否已创建。"
      : "取消请求超时，无法确认后端是否已收到；请刷新任务状态后再决定是否重试。";
  }
  return messageFor(error);
}
function readIds() {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(storageKey) ?? "[]");
    return Array.isArray(value)
      ? value.filter((id): id is string => typeof id === "string").slice(0, 30)
      : [];
  } catch {
    return [];
  }
}
function saveIds(ids: string[]) {
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(ids.slice(0, 30)));
  } catch {
    // Private browsing or storage limits do not block a task in the current session.
  }
}
function HistoryPage() {
  const token = useSyncExternalStore(subscribeSession, getAccessToken, () => null);
  const [hydrated, setHydrated] = useState(false);
  const [jobs, setJobs] = useState<HistoryJob[]>([]);
  const [symbol, setSymbol] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [today, setToday] = useState("");
  const [now, setNow] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [jobErrors, setJobErrors] = useState<Record<string, string>>({});
  const [unresolvedIds, setUnresolvedIds] = useState<string[]>([]);
  const resultRequests = useRef(new Map<string, AbortController>());
  const [resultBusy, setResultBusy] = useState<string[]>([]);
  const [resultErrors, setResultErrors] = useState<Record<string, string>>({});
  const [results, setResults] = useState<Record<string, HistoryResults>>({});
  const request = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  useEffect(() => {
    const currentDate = shanghaiToday();
    setToday(currentDate);
    setStartDate(dateDaysAgo(365, currentDate));
    setEndDate(dateDaysAgo(1, currentDate));
    setNow(Date.now());
    setHydrated(true);
  }, []);
  useEffect(
    () => () => {
      request.current?.abort();
      resultRequests.current.forEach((controller) => controller.abort());
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

  useEffect(() => {
    if (!hydrated || !token) return;
    const controller = new AbortController();
    request.current?.abort();
    request.current = controller;
    const ids = readIds();
    void Promise.all(
      ids.map(async (id) => {
        try {
          return { id, job: await reload(id, controller.signal), error: "" };
        } catch (reason) {
          return { id, job: null, error: abortError(reason) ? "" : messageFor(reason) };
        }
      }),
    ).then((records) => {
      if (controller.signal.aborted) return;
      const available = records.flatMap(({ job }) => (job ? [job] : []));
      const unresolved = records.filter(({ job }) => !job).map(({ id }) => id);
      setJobs(available);
      setUnresolvedIds(unresolved);
      setJobErrors(
        Object.fromEntries(
          records.filter(({ error }) => error).map(({ id, error }) => [id, error]),
        ),
      );
      saveIds(ids);
    });
    return () => controller.abort();
  }, [hydrated, token, reload]);
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
        if (changed) saveIds(next.map((job) => job.id).concat(unresolvedIds));
        return changed ? next : current;
      });
      timer = setTimeout(() => void poll(), 2500);
    };
    timer = setTimeout(() => void poll(), 2500);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [jobs, reload, token, unresolvedIds]);

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
        setJobs((current) => {
          const next = [job, ...current.filter((item) => item.id !== job.id)].slice(0, 30);
          saveIds([...next.map((item) => item.id), ...unresolvedIds]);
          return next;
        });
        setUnresolvedIds((current) => current.filter((id) => id !== job.id));
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

  const fetchResults = async (job: HistoryJob, offset = 0) => {
    if (!token || resultRequests.current.has(job.id)) return;
    const controller = new AbortController();
    resultRequests.current.set(job.id, controller);
    setResultBusy((current) => (current.includes(job.id) ? current : [...current, job.id]));
    setResultErrors((current) => ({ ...current, [job.id]: "" }));
    try {
      const page = await getHistoryResults(token, job.id, offset, requestSignal(controller.signal));
      setResults((current) => ({ ...current, [job.id]: page }));
    } catch (reason) {
      if (!abortError(reason))
        setResultErrors((current) => ({ ...current, [job.id]: messageFor(reason) }));
    } finally {
      resultRequests.current.delete(job.id);
      setResultBusy((current) => current.filter((id) => id !== job.id));
    }
  };
  const refreshTask = (jobId: string) =>
    runRequest(async (signal) => {
      try {
        const updated = await reload(jobId, signal);
        setJobs((current) => [updated, ...current.filter((job) => job.id !== updated.id)]);
        setUnresolvedIds((current) => current.filter((id) => id !== jobId));
        setJobErrors((current) => {
          const next = { ...current };
          delete next[jobId];
          return next;
        });
        saveIds([
          updated.id,
          ...jobs.filter((job) => job.id !== updated.id).map((job) => job.id),
          ...unresolvedIds.filter((id) => id !== updated.id),
        ]);
      } catch (reason) {
        if (!abortError(reason))
          setJobErrors((current) => ({ ...current, [jobId]: messageFor(reason) }));
      }
    });
  const removeUnresolved = (jobId: string) => {
    setUnresolvedIds((current) => current.filter((id) => id !== jobId));
    setJobErrors((current) => {
      const next = { ...current };
      delete next[jobId];
      return next;
    });
    saveIds([...jobs.map((job) => job.id), ...unresolvedIds.filter((id) => id !== jobId)]);
  };

  const remove = (jobId: string) => {
    setJobs((current) => {
      const next = current.filter((job) => job.id !== jobId);
      saveIds([...next.map((job) => job.id), ...unresolvedIds]);
      return next;
    });
    setResults((current) => {
      const next = { ...current };
      delete next[jobId];
      return next;
    });
    setJobErrors((current) => {
      const next = { ...current };
      delete next[jobId];
      return next;
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
                任务成功后可分页查看后端保存的日线记录；成功不代表每个交易日都有数据。
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
            <section className="history-jobs" aria-labelledby="history-jobs-title">
              <div className="history-section-heading">
                <div>
                  <h2 id="history-jobs-title">任务记录</h2>
                  <p>刷新后从本浏览器保存的任务编号恢复状态；后端不提供完整任务列表。</p>
                </div>
                <span>{jobs.length + unresolvedIds.length} 项</span>
              </div>
              {jobs.length === 0 && unresolvedIds.length === 0 ? (
                <div className="history-empty">
                  <h3>尚无已保存的任务</h3>
                  <p>创建任务后，状态和结果会显示在此处。</p>
                </div>
              ) : (
                <div className="history-job-list">
                  {unresolvedIds.map((id) => (
                    <article className="history-job" key={id}>
                      <div className="history-job-head">
                        <div>
                          <h3>无法确认任务状态</h3>
                          <p>
                            任务编号：<code>{id}</code>
                          </p>
                        </div>
                        <span className="history-status is-failed">状态未知</span>
                      </div>
                      <p className="history-error" role="alert">
                        {jobErrors[id] || "尚未从后端取得此任务的状态。"}{" "}
                        页面不会将其显示为成功或失败。
                      </p>
                      <div className="history-job-actions">
                        <button
                          className="trade-import-secondary"
                          disabled={busy}
                          onClick={() => refreshTask(id)}
                        >
                          重新查询
                        </button>
                        <button className="history-remove" onClick={() => removeUnresolved(id)}>
                          从此浏览器移除
                        </button>
                      </div>
                    </article>
                  ))}
                  {jobs.map((job) => {
                    const progress =
                      job.total_chunks > 0 &&
                      job.completed_chunks >= 0 &&
                      job.completed_chunks <= job.total_chunks
                        ? job.status === "succeeded"
                          ? 100
                          : Math.min(
                              99,
                              Math.floor((job.completed_chunks / job.total_chunks) * 100),
                            )
                        : null;
                    const result = results[job.id];
                    return (
                      <article className="history-job" key={job.id}>
                        <div className="history-job-head">
                          <div>
                            <h3>{job.symbol}</h3>
                            <p>
                              {job.start_date} 至 {job.end_date}
                            </p>
                          </div>
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
                        {!terminal.has(job.status) && (
                          <div className="history-progress" aria-live="polite">
                            {progress !== null && job.status === "running" ? (
                              <>
                                <div className="history-progress-label">
                                  <span>{stageLabels[job.stage] ?? "任务正在处理"}</span>
                                  <span>{progress}%</span>
                                </div>
                                <progress
                                  max={100}
                                  value={progress}
                                  aria-label={`任务进度 ${progress}%`}
                                />
                              </>
                            ) : (
                              <p>
                                {job.cancel_requested
                                  ? "取消请求已送达，等待后端停止当前工作。"
                                  : "正在等待后端处理；当前没有可用的精确百分比。"}
                              </p>
                            )}
                          </div>
                        )}
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
                              disabled={resultBusy.includes(job.id) || busy}
                              onClick={() => cancel(job)}
                            >
                              {job.cancel_requested ? "正在取消…" : "请求取消"}
                            </button>
                          )}
                          {job.status === "succeeded" && (
                            <button
                              className="trade-import-secondary"
                              disabled={resultBusy.includes(job.id)}
                              onClick={() =>
                                result ? fetchResults(job, result.offset) : fetchResults(job)
                              }
                            >
                              {resultBusy.includes(job.id)
                                ? "正在读取…"
                                : result
                                  ? "刷新结果"
                                  : "查看结果"}
                            </button>
                          )}
                          {job.status === "failed" && (
                            <button
                              className="trade-import-secondary"
                              disabled={busy}
                              onClick={() => {
                                setSymbol(job.symbol);
                                setStartDate(job.start_date);
                                setEndDate(job.end_date);
                                setNotice(
                                  "已载入原任务参数；请确认后创建新的拉取任务。后端不支持原任务重试。",
                                );
                                window.scrollTo({ top: 0, behavior: "smooth" });
                              }}
                            >
                              重新拉取
                            </button>
                          )}
                          {terminal.has(job.status) && (
                            <button className="history-remove" onClick={() => remove(job.id)}>
                              从此浏览器移除
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
                        {resultErrors[job.id] && (
                          <p className="history-error" role="alert">
                            {resultErrors[job.id]}
                          </p>
                        )}
                        {resultErrors[job.id] && results[job.id] && (
                          <p className="history-muted">
                            以下为此前成功读取的结果；本次读取未成功，数据可能不是最新页。
                          </p>
                        )}
                        {result && (
                          <div className="history-results">
                            <div className="history-result-heading">
                              <h4>行情结果</h4>
                              <span>共 {result.total} 条 · 按日期升序</span>
                            </div>
                            {result.bars.length === 0 ? (
                              <p className="history-muted">后端未返回日线记录。</p>
                            ) : (
                              <div className="history-table-wrap">
                                <table>
                                  <thead>
                                    <tr>
                                      <th>交易日</th>
                                      <th>开盘</th>
                                      <th>最高</th>
                                      <th>最低</th>
                                      <th>收盘</th>
                                      <th>成交量</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {result.bars.map((bar) => (
                                      <tr key={bar.timestamp}>
                                        <td>{bar.timestamp?.slice(0, 10) ?? "—"}</td>
                                        <td>{bar.open}</td>
                                        <td>{bar.high}</td>
                                        <td>{bar.low}</td>
                                        <td>{bar.close}</td>
                                        <td>{Number(bar.volume).toLocaleString("zh-CN")}</td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            )}
                            <div className="history-pagination">
                              <span>
                                {result.total === 0
                                  ? "0 条"
                                  : `${result.offset + 1}–${Math.min(result.offset + result.bars.length, result.total)} / ${result.total} 条`}
                              </span>
                              <div>
                                <button
                                  className="trade-import-secondary"
                                  disabled={result.offset <= 0 || resultBusy.includes(job.id)}
                                  onClick={() =>
                                    fetchResults(job, Math.max(0, result.offset - pageSize))
                                  }
                                >
                                  上一页
                                </button>
                                <button
                                  className="trade-import-secondary"
                                  disabled={
                                    result.offset + result.bars.length >= result.total ||
                                    resultBusy.includes(job.id)
                                  }
                                  onClick={() => fetchResults(job, result.offset + pageSize)}
                                >
                                  下一页
                                </button>
                              </div>
                            </div>
                          </div>
                        )}
                      </article>
                    );
                  })}
                </div>
              )}
            </section>
          </>
        )}
      </div>
    </AppShell>
  );
}
export default function HistoryRoute() {
  return <HistoryPage />;
}
