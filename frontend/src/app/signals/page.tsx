"use client";

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { AppShell } from "@/components/app-shell";
import { PageHeading } from "@/components/page-heading";
import { ApiError, getSignalSnapshot, streamSignals, type Signal } from "@/lib/a500-client";
import { beginAuthorization, configurationReady } from "@/lib/oidc";
import { clearAccessToken, getAccessToken, subscribeSession } from "@/lib/session";
import { mergeSignal, signalFromEvent } from "@/lib/signal-state";

function timeLabel(value: string | null) {
  if (!value) return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "时间无效";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).format(date);
}

function statusLabel(signal: Signal) {
  if (signal.stale || signal.status.toUpperCase() === "UNAVAILABLE") return "数据不可用";
  if (!signal.direction) return signal.status.toUpperCase() === "WAIT" ? "等待信号" : signal.status;
  return signal.direction === "BUY"
    ? "偏多参考"
    : signal.direction === "SELL"
      ? "偏空参考"
      : signal.direction;
}
function errorLabel(reason: unknown) {
  if (reason instanceof ApiError && reason.status === 422)
    return "标的筛选请求被拒绝（422）。请取消筛选并确认标的配置。";
  return reason instanceof Error ? reason.message : "读取信号失败。";
}

export default function SignalsPage() {
  const token = useSyncExternalStore(subscribeSession, getAccessToken, () => null);
  const [rows, setRows] = useState<Signal[]>([]);
  const [symbolUniverse, setSymbolUniverse] = useState<string[]>([]);
  const [metadata, setMetadata] = useState<{ strategy: string; fingerprint: string } | null>(null);
  const [filterReady, setFilterReady] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [loadedToken, setLoadedToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [streamStatus, setStreamStatus] = useState("等待连接");
  const [lastRead, setLastRead] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(
    async (accessToken: string, symbols?: string[], signal?: AbortSignal) => {
      const result = await getSignalSnapshot(accessToken, symbols, signal);
      if (signal?.aborted) return;
      setRows(result.signals);
      if (!symbols) setSymbolUniverse(result.signals.map(({ symbol }) => symbol));
      setMetadata({ strategy: result.strategy, fingerprint: result.parameter_fingerprint });
      setLastRead(new Date().toISOString());
      setError(null);
      setFilterReady(true);
    },
    [],
  );

  useEffect(() => {
    if (!token) return;
    const accessToken = token;
    const controller = new AbortController();
    queueMicrotask(() => {
      void refresh(accessToken, undefined, controller.signal)
        .catch((reason: unknown) => {
          if (controller.signal.aborted) return;
          if (reason instanceof ApiError && reason.status === 401) clearAccessToken();
          setError(errorLabel(reason));
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoadedToken(accessToken);
        });
    });
    return () => controller.abort();
  }, [token, refresh]);

  const isLoading = token !== null && loadedToken !== token;
  const configuredSymbols = symbolUniverse;

  useEffect(() => {
    if (!token || !filterReady || configuredSymbols.length === 0) return;
    const accessToken = token;
    const symbols = selected.length ? selected : configuredSymbols;
    const controller = new AbortController();
    let cursor: string | undefined;
    let active = true;
    const isAbort = (reason: unknown) =>
      reason instanceof DOMException && reason.name === "AbortError";
    const wait = (milliseconds: number) =>
      new Promise<void>((resolve) => {
        const timeout = window.setTimeout(resolve, milliseconds);
        controller.signal.addEventListener(
          "abort",
          () => {
            window.clearTimeout(timeout);
            resolve();
          },
          { once: true },
        );
      });
    async function run() {
      while (active) {
        if (cursor) {
          try {
            await refresh(accessToken, symbols, controller.signal);
          } catch (reason) {
            if (controller.signal.aborted) return;
            setError(errorLabel(reason));
          }
        }
        setStreamStatus(cursor ? "恢复实时订阅" : "正在连接实时订阅");
        try {
          await streamSignals(
            accessToken,
            symbols,
            controller.signal,
            (event) => {
              if (event.id) cursor = event.id;
              if (event.event === "reset") {
                cursor = undefined;
                setStreamStatus("游标重置，正在重新读取当前状态");
                void refresh(accessToken, symbols, controller.signal).catch((reason: unknown) => {
                  if (!controller.signal.aborted) {
                    if (reason instanceof ApiError && reason.status === 401) clearAccessToken();
                    setError(errorLabel(reason));
                  }
                });
                return;
              }
              if (event.event === "snapshot") {
                setStreamStatus("实时订阅");
                const signal = signalFromEvent(event.data);
                if (signal) setRows((current) => mergeSignal(current, signal));
                return;
              }
              if (
                ["buy", "sell", "buyback", "take_profit", "hold", "wait"].includes(
                  event.event.toLowerCase(),
                )
              ) {
                // A replayed direction event is history, not the authoritative current suggestion.
                setStreamStatus("实时订阅");
                void refresh(accessToken, symbols, controller.signal).catch((reason: unknown) => {
                  if (!controller.signal.aborted) {
                    if (reason instanceof ApiError && reason.status === 401) clearAccessToken();
                    setError(errorLabel(reason));
                  }
                });
              }
              if (event.event === "unavailable") setStreamStatus("实时服务暂不可用");
            },
            cursor,
          );
          if (!active) return;
          setRows([]);
          setStreamStatus("连接中断，5 秒后重连");
        } catch (reason) {
          if (!active || isAbort(reason)) return;
          if (reason instanceof ApiError && reason.status === 401) {
            clearAccessToken();
            setError("授权已失效，请重新安全连接。");
            return;
          }
          setRows([]);
          setStreamStatus(
            reason instanceof ApiError && reason.status === 503
              ? "实时服务暂不可用"
              : "连接中断，5 秒后重连",
          );
        }
        await wait(5000);
      }
    }
    void run();
    return () => {
      active = false;
      controller.abort();
    };
  }, [token, filterReady, selected, refresh, configuredSymbols]);

  async function connect() {
    if (!configurationReady) {
      setError("身份服务尚未配置，请联系部署管理员。");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      sessionStorage.setItem(
        "a500.auth.return",
        `${window.location.pathname}${window.location.search}`,
      );
      const url = await beginAuthorization();
      window.location.assign(url);
    } catch (reason) {
      sessionStorage.removeItem("a500.auth.return");
      setError(reason instanceof Error ? reason.message : "无法启动安全连接。");
      setBusy(false);
    }
  }

  function applySymbols(symbols: string[]) {
    setSelected(symbols);
    if (token)
      void refresh(token, symbols.length ? symbols : undefined).catch((reason: unknown) => {
        if (reason instanceof ApiError && reason.status === 401) clearAccessToken();
        setError(errorLabel(reason));
      });
  }

  function toggleSymbol(symbol: string) {
    applySymbols(
      selected.includes(symbol)
        ? selected.filter((item) => item !== symbol)
        : [...selected, symbol],
    );
  }

  return (
    <AppShell activePath="/signals" connected={Boolean(token)} onConnect={() => void connect()}>
      <section className="signal-page" aria-labelledby="signals-title">
        <PageHeading
          className="page-heading"
          title="策略信号"
          titleId="signals-title"
          description="查看已配置标的的当前策略状态、依据与数据时效。"
          descriptionClassName="page-intro"
        >
          <button
            className="refresh-button"
            disabled={!token || isLoading}
            onClick={() =>
              token &&
              void refresh(token, selected.length ? selected : undefined).catch(
                (reason: unknown) => {
                  if (reason instanceof ApiError && reason.status === 401) clearAccessToken();
                  setError(errorLabel(reason));
                },
              )
            }
          >
            刷新当前状态
          </button>
        </PageHeading>
        <div className="signal-toolbar">
          <div className="stream-state" role="status">
            <span
              className={`session-indicator${streamStatus === "实时订阅" ? " is-connected" : ""}`}
              aria-hidden="true"
            />
            实时更新：{streamStatus}
          </div>
          <div className="toolbar-meta">
            {lastRead && (
              <span>
                最近读取 <time>{timeLabel(lastRead)}</time>
              </span>
            )}
            {metadata && (
              <details className="strategy-details">
                <summary>策略与参数版本</summary>
                <div className="strategy-popover">
                  <b>{metadata.strategy}</b>
                  <span>规则版本随标的展示</span>
                  <span>
                    参数指纹 <code>{metadata.fingerprint}</code>
                  </span>
                </div>
              </details>
            )}
          </div>
        </div>
        {!token && (
          <section className="state-panel" aria-label="需要身份认证">
            <h2>连接后查看策略信号</h2>
            <p>通过 Keycloak 安全授权后，页面将读取真实信号数据。令牌仅保留在当前标签页内存中。</p>
            <button
              className="refresh-button primary"
              onClick={() => void connect()}
              disabled={busy || !configurationReady}
            >
              {busy ? "正在连接…" : "安全连接"}
            </button>
            {!configurationReady && <p role="alert">身份服务尚未配置，请联系部署管理员。</p>}
          </section>
        )}
        {error && (
          <div className="signal-error" role="alert">
            <p>{error}</p>
            {error.includes("422") && <p>筛选中包含未配置标的；请清除筛选后重新读取。</p>}
            <button
              onClick={() =>
                token &&
                void refresh(token, selected.length ? selected : undefined).catch(
                  (reason: unknown) => {
                    if (reason instanceof ApiError && reason.status === 401) clearAccessToken();
                    setError(errorLabel(reason));
                  },
                )
              }
            >
              重试
            </button>
          </div>
        )}
        {isLoading && token && (
          <div className="state-panel" role="status">
            正在读取已配置标的…
          </div>
        )}
        {!isLoading && token && !error && rows.length === 0 && (
          <div className="state-panel">
            <h2>
              {selected.length
                ? "当前筛选没有可显示信号"
                : streamStatus.includes("断") || streamStatus.includes("不可用")
                  ? "实时信号暂不可用"
                  : "暂无已配置标的"}
            </h2>
            <p>
              {streamStatus.includes("断")
                ? "实时连接已中断，旧信号已清除；正在尝试恢复。"
                : "信号服务当前未返回标的状态；没有使用示例数据填充。"}
            </p>
            {selected.length > 0 && (
              <button className="refresh-button" onClick={() => applySymbols([])}>
                清除标的筛选
              </button>
            )}
          </div>
        )}
        {token && rows.length > 0 && (
          <>
            <div className="signal-controls">
              <span>标的范围</span>
              <details className="symbol-filter">
                <summary>
                  {selected.length
                    ? `已选 ${selected.length} / ${configuredSymbols.length} 个`
                    : `全部标的 · ${configuredSymbols.length}`}
                </summary>
                <div className="filter-options">
                  <button onClick={() => applySymbols([])}>全部标的</button>
                  {configuredSymbols.map((symbol) => (
                    <label key={symbol}>
                      <input
                        type="checkbox"
                        checked={!selected.length || selected.includes(symbol)}
                        onChange={() => toggleSymbol(symbol)}
                      />
                      {symbol}
                    </label>
                  ))}
                </div>
              </details>
              <span>REST 与实时订阅均按所选标的筛选</span>
            </div>
            <div className="signals-table-scroll">
              <table className="signals-table" role="table">
                <thead role="rowgroup">
                  <tr role="row">
                    <th scope="col" role="columnheader">
                      标的 / 策略
                    </th>
                    <th scope="col" role="columnheader">
                      当前状态
                    </th>
                    <th scope="col" role="columnheader">
                      建议数量
                    </th>
                    <th scope="col" role="columnheader">
                      信号原因
                    </th>
                    <th scope="col" role="columnheader">
                      信号时间 / 时效
                    </th>
                    <th scope="col" role="columnheader">
                      执行资格
                    </th>
                  </tr>
                </thead>
                <tbody role="rowgroup">
                  {rows
                    .filter((signal) => !selected.length || selected.includes(signal.symbol))
                    .map((signal) => {
                      const unavailable =
                        signal.stale || signal.status.toUpperCase() === "UNAVAILABLE";
                      return (
                        <tr
                          key={signal.symbol}
                          className={unavailable ? "is-unavailable" : ""}
                          role="row"
                        >
                          <th scope="row" role="rowheader">
                            <span className="table-symbol">{signal.symbol}</span>
                            <span className="strategy-version">
                              {signal.rule_version} · {signal.strategy_identity}
                            </span>
                            <details className="row-parameters">
                              <summary>参数</summary>
                              <pre>{JSON.stringify(signal.parameters, null, 2)}</pre>
                            </details>
                          </th>
                          <td role="cell" data-label="当前状态">
                            <span
                              className={`signal-status ${unavailable ? "status-unavailable" : signal.direction ? "status-direction" : "status-wait"}`}
                            >
                              {statusLabel(signal)}
                            </span>
                            <span className="direction-detail">
                              {unavailable
                                ? "不作为当前建议"
                                : signal.direction
                                  ? `方向：${signal.direction}`
                                  : "当前无方向"}
                            </span>
                          </td>
                          <td role="cell" data-label="建议数量" className="quantity">
                            {unavailable || signal.quantity == null
                              ? "—"
                              : signal.quantity.toLocaleString("zh-CN")}
                          </td>
                          <td role="cell" data-label="信号原因" className="reason-list">
                            {signal.reasons.length ? (
                              signal.reasons.map((reason, index) => (
                                <span key={`${index}-${reason}`}>{reason}</span>
                              ))
                            ) : (
                              <span>未提供原因</span>
                            )}
                          </td>
                          <td role="cell" data-label="信号时间 / 时效">
                            <time className="event-time">{timeLabel(signal.bar_timestamp)}</time>
                            <span className="freshness">
                              {unavailable ? "已过期或不可用" : "当前状态 · 以事件时间为准"}
                            </span>
                          </td>
                          <td role="cell" data-label="执行资格">
                            <span
                              className={`eligibility ${signal.execution_eligible && !unavailable ? "eligible" : "ineligible"}`}
                            >
                              {signal.execution_eligible && !unavailable
                                ? "符合策略资格"
                                : "不具备执行资格"}
                            </span>
                            <span className="eligibility-note">非下单指令</span>
                          </td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            </div>
            {metadata && (
              <p className="parameter-line">
                参数指纹 <code>{metadata.fingerprint}</code> · 每个标的的完整参数由 API 提供。
              </p>
            )}
          </>
        )}
      </section>
    </AppShell>
  );
}
