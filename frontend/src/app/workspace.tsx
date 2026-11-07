"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  ApiError,
  abortableDelay,
  confirmPreview,
  getNav,
  getPreview,
  getSignals,
  getSnapshot,
  saveSnapshot,
  streamSignals,
  uploadPreview,
  type AccountNav,
  type ImportPreview,
  type Signal,
  type SnapshotResult,
} from "@/lib/a500-client";
import { beginAuthorization, completeAuthorization, configurationReady } from "@/lib/oidc";
import { parseSnapshotDraft, snapshotDraft, snapshotFields } from "@/lib/snapshot-draft";

const STREAM_DISCONNECTED = "尚未连接";
const STREAM_NO_SYMBOLS = "当前没有可订阅的标的";
const STREAM_LIVE = "实时订阅";
const STREAM_RECONNECT_DELAY_MS = 5000;


function formatTime(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23",
      }).format(date);
}
function signalLabel(signal: Signal) {
  if (signal.stale || signal.status === "UNAVAILABLE") return "数据不可用";
  return signal.direction || signal.status || "等待信号";
}
// The server-rendered button must not accept clicks before React attaches its handler.
const subscribeHydration = () => () => {};
const clientHydrated = () => true;
const serverHydrated = () => false;

export default function Workspace() {
  const hydrated = useSyncExternalStore(subscribeHydration, clientHydrated, serverHydrated);
  const [token, setToken] = useState<string | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [nav, setNav] = useState<AccountNav | null>(null);
  const [navError, setNavError] = useState<string | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [snapshotBusy, setSnapshotBusy] = useState(false);
  const [snapshotKnown, setSnapshotKnown] = useState(false);
  const [etag, setEtag] = useState<string | null>(null);
  const [latest, setLatest] = useState<SnapshotResult | null | undefined>(undefined);
  const [draft, setDraft] = useState(snapshotDraft);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [streamState, setStreamState] = useState<string>(STREAM_DISCONNECTED);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const authAbort = useRef<AbortController | null>(null);
  const sessionAbort = useRef<AbortController | null>(null);
  const identity = useRef(0);
  const expiryTimer = useRef<number | undefined>(undefined);
  const draftDirty = useRef(false);
  const clearSession = useCallback(() => {
    identity.current += 1;
    authAbort.current?.abort();
    window.clearTimeout(expiryTimer.current);
    sessionAbort.current?.abort();
    setToken(null);
    setSignals([]);
    setNav(null);
    setNavError(null);
    setPreview(null);
    setFile(null);
    setLastUpdated(null);
    setDraft(snapshotDraft());
    draftDirty.current = false;
    setSnapshotKnown(false);
    setEtag(null);
    setLatest(undefined);
    setConflict(false);
    setSnapshotError(null);
    setSnapshotBusy(false);
    setBusy(false);
    setNotice(null);
    setStreamState(STREAM_DISCONNECTED);
  }, []);
  const fail = useCallback(
    (reason: unknown) => {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      if (reason instanceof ApiError && reason.status === 401) clearSession();
      setError(reason instanceof Error ? reason.message : "服务请求失败。");
    },
    [clearSession],
  );
  useEffect(() => {
    const generation = identity.current;
    const pending = completeAuthorization();
    if (!pending) return;
    void Promise.resolve().then(() => {
      if (generation === identity.current) setBusy(true);
    });
    void pending.then(({ token: accessToken, expiresIn }) => {
      if (generation !== identity.current) return;
      setSnapshotBusy(true);
      setToken(accessToken);
      setNotice("数据服务已连接。");
      expiryTimer.current = window.setTimeout(() => {
        clearSession();
        setError("授权已过期，请重新安全连接。");
      }, Math.min(expiresIn * 1000, 2147483647));
    }).catch((reason) => {
      if (generation === identity.current) fail(reason);
    }).finally(() => {
      if (generation === identity.current) setBusy(false);
    });
  }, [clearSession, fail]);


  useEffect(() => {
    if (!token) return;
    const accessToken = token;
    const controller = new AbortController();
    sessionAbort.current = controller;
    const active = () => !controller.signal.aborted;
    const signal = controller.signal;
    let cursor: string | undefined;
    let requeryRunning = false;
    let requeryAgain = false;
    async function refreshSignals() {
      if (requeryRunning) {
        requeryAgain = true;
        return;
      }
      requeryRunning = true;
      try {
        do {
          requeryAgain = false;
          const rows = await getSignals(accessToken, signal);
          if (active()) {
            setSignals(rows);
            setLastUpdated(new Date().toISOString());
          }
        } while (requeryAgain && active());
      } catch (reason) {
        if (active()) {
          setSignals([]);
          setStreamState("读取信号失败");
          fail(reason);
        }
      } finally {
        requeryRunning = false;
      }
    }
    async function connectStream() {
      while (active()) {
        try {
          const current = await getSignals(accessToken, signal);
          if (!active()) return;
          setSignals(current);
          setLastUpdated(new Date().toISOString());
          if (!current.length) {
            setStreamState(STREAM_NO_SYMBOLS);
          } else {
            setStreamState("连接中");
            await streamSignals(
              accessToken,
              current.map((row) => row.symbol),
              signal,
              (event) => {
                if (!active()) return;
                if (event.id !== undefined) cursor = event.id || undefined;
                if (event.event === "reset") {
                  cursor = undefined;
                  setNotice("信号游标已重置；正在重新读取当前信号。");
                }
                setStreamState(event.event === "unavailable" ? "信号服务不可用" : STREAM_LIVE);
                // Stream events include retained historical directional states. Never render
                // those as current advice: requery the freshness-checked current endpoint.
                void refreshSignals();
              },
              cursor,
            );
            if (active()) {
              setSignals([]);
              setStreamState("连接中断");
            }
          }
        } catch (reason) {
          if (!active()) return;
          setSignals([]);
          setStreamState("连接中断");
          fail(reason);
          if (reason instanceof ApiError && reason.status === 401) return;
        }
        try {
          await abortableDelay(STREAM_RECONNECT_DELAY_MS, signal);
        } catch {
          return;
        }
      }
    }
    void connectStream();
    void getNav(token, signal)
      .then((value) => {
        if (active()) setNav(value);
      })
      .catch((reason) => {
        if (active()) {
          setNavError(String(reason.message || reason));
          fail(reason);
        }
      });
    void getSnapshot(token, signal)
      .then((value) => {
        if (!active()) return;
        setSnapshotKnown(true);
        setEtag(value?.etag ?? null);
        if (!draftDirty.current) setDraft(snapshotDraft(value?.snapshot));
        else setLatest(value);
      })
      .catch((reason) => {
        if (active()) {
          setSnapshotError(String(reason.message || reason));
          fail(reason);
        }
      })
      .finally(() => {
        if (active()) setSnapshotBusy(false);
      });
    const batchId = window.localStorage.getItem("a500.import-batch-id");
    if (batchId)
      void getPreview(token, batchId, signal)
        .then((value) => {
          if (active()) {
            setPreview(value);
            setNotice("已恢复导入批次。导入不会更新账户快照。");
          }
        })
        .catch((reason) => {
          if (!active()) return;
          if (reason instanceof ApiError && reason.status === 404) {
            window.localStorage.removeItem("a500.import-batch-id");
            setNotice("导入批次不存在、已过期或不属于当前账户，请重新预览文件。");
          } else fail(reason);
        });
    return () => controller.abort();
  }, [token, fail]);
  useEffect(
    () => () => {
      authAbort.current?.abort();
      sessionAbort.current?.abort();
      window.clearTimeout(expiryTimer.current);
    },
    [],
  );

  async function connect() {
    clearSession();
    const generation = identity.current;
    const controller = new AbortController();
    authAbort.current = controller;
    setBusy(true);
    setError(null);
    setStreamState("连接中");
    try {
      const url = await beginAuthorization(controller.signal);
      if (generation !== identity.current || controller.signal.aborted) return;
      window.location.assign(url);
    } catch (reason) {
      if (generation === identity.current) fail(reason);
    } finally {
      if (generation === identity.current) setBusy(false);
    }
  }
  function disconnect() {
    clearSession();
    setError(null);
    setNotice("已断开；访问令牌已从页面内存清除。");
  }
  async function refreshNav() {
    if (!token) return;
    const generation = identity.current;
    setNav(null);
    setNavError(null);
    try {
      const value = await getNav(token, sessionAbort.current?.signal);
      if (generation === identity.current) setNav(value);
    } catch (reason) {
      if (generation === identity.current) {
        setNavError(String((reason as Error).message));
        fail(reason);
      }
    }
  }
  async function readLatest() {
    if (!token) return;
    const generation = identity.current;
    setSnapshotBusy(true);
    setSnapshotError(null);
    try {
      const value = await getSnapshot(token, sessionAbort.current?.signal);
      if (generation !== identity.current) return;
      setLatest(value);
      setSnapshotKnown(true);
      setNotice("已读取最新快照，当前草稿未修改。请明确选择替换草稿或以最新版本继续编辑。");
      await refreshNav();
    } catch (reason) {
      if (generation === identity.current) {
        setSnapshotError(String((reason as Error).message));
        fail(reason);
      }
    } finally {
      if (generation === identity.current) setSnapshotBusy(false);
    }
  }
  async function saveAccount() {
    if (!token || !snapshotKnown) return;
    const generation = identity.current;
    setSnapshotBusy(true);
    setSnapshotError(null);
    try {
      const result = await saveSnapshot(
        token,
        parseSnapshotDraft(draft),
        etag,
        sessionAbort.current?.signal,
      );
      if (generation !== identity.current) return;
      setEtag(result.etag);
      setLatest(undefined);
      setConflict(false);
      setDraft(snapshotDraft(result.snapshot));
      draftDirty.current = false;
      setNotice("账户快照已保存；正在刷新净资产。");
      await refreshNav();
    } catch (reason) {
      if (generation !== identity.current) return;
      if (reason instanceof ApiError && (reason.status === 412 || reason.status === 428)) {
        setConflict(true);
        setSnapshotError("快照版本冲突，草稿已保留。请读取最新快照后选择如何继续。");
      } else {
        setSnapshotError(String((reason as Error).message));
        fail(reason);
      }
    } finally {
      if (generation === identity.current) setSnapshotBusy(false);
    }
  }
  async function previewFile() {
    if (!token || !file) return;
    const generation = identity.current;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const value = await uploadPreview(token, file, sessionAbort.current?.signal);
      if (generation !== identity.current) return;
      setPreview(value);
      window.localStorage.setItem("a500.import-batch-id", value.batch_id);
    } catch (reason) {
      if (generation === identity.current) fail(reason);
    } finally {
      if (generation === identity.current) setBusy(false);
    }
  }
  async function confirmImport() {
    if (!token || !preview) return;
    const generation = identity.current;
    setBusy(true);
    setError(null);
    try {
      const result = await confirmPreview(token, preview.batch_id, sessionAbort.current?.signal);
      if (generation !== identity.current) return;
      setPreview(result);
      setNotice("导入已确认；成交为用户提供、未经系统核实。导入不会更新账户快照。");
    } catch (reason) {
      if (generation === identity.current) fail(reason);
    } finally {
      if (generation === identity.current) setBusy(false);
    }
  }
  if (!token) {
    return (
      <div className="shell login-shell">
        <header className="masthead login-masthead">
          <div className="brand" aria-label="A500 Trader">
            <span className="brand-mark">A</span>
            <span>
              A500 <b>TRADER</b>
            </span>
          </div>
          <span className="login-service">
            <span className="status-dot" aria-hidden="true" /> 数据服务入口
          </span>
        </header>
        <main className="login-layout" aria-labelledby="login-title">
          <section className="login-primary">
            <h1 id="login-title">
              连接你的
              <br />
              投资工作台
            </h1>
            <p className="login-intro">
              使用已配置的身份提供方授权。登录后，实时信号与账户数据仅对当前认证主体可见。
            </p>
            {!configurationReady && (
              <div className="config-note login-config" role="status">
                需配置 <code>NEXT_PUBLIC_A500_OIDC_ISSUER</code> 后连接身份服务。API 使用同源{" "}
                <code>/api/v1</code>。
              </div>
            )}
            {error && (
              <div className="notice error login-error" role="alert">
                {error}
              </div>
            )}
            <section className="auth-box" aria-labelledby="auth-title" aria-live="polite">
              <div className="auth-box-heading">
                <div>
                  <h2 id="auth-title">通过身份提供方继续</h2>
                  <p>我们会安全地将你转至已配置的身份提供方。</p>
                </div>
                <span className={`auth-state ${busy ? "is-active" : ""}`}>
                  <span className="status-dot" aria-hidden="true" />
                  {busy ? "等待授权" : "尚未连接"}
                </span>
              </div>
              <button
                className="button primary full auth-action"
                onClick={() => void connect()}
                disabled={!hydrated || busy || !configurationReady}
              >
                {busy ? "正在连接…" : "安全连接"}
                {!busy && <span aria-hidden="true">→</span>}
              </button>
              {busy && (
                <button className="text-button auth-cancel" onClick={disconnect}>
                  取消连接
                </button>
              )}
              <p className="auth-footnote">
                访问令牌仅保存在当前页面内存中；关闭页面或断开连接后即清除。
              </p>
            </section>
          </section>
          <aside className="login-aside" aria-label="授权说明">
            <div className="aside-rule" />
            <h2>一次授权，开始查看</h2>
            <ol className="login-steps">
              <li>
                <span>1</span>
                <div>
                  <strong>连接身份提供方</strong>
                  <p>由系统配置的 OIDC 服务验证身份。</p>
                </div>
              </li>
              <li>
                <span>2</span>
                <div>
                  <strong>登录并授权</strong>
                  <p>在身份提供方页面完成本次登录。</p>
                </div>
              </li>
              <li>
                <span>3</span>
                <div>
                  <strong>返回工作台</strong>
                  <p>授权完成后自动载入服务数据。</p>
                </div>
              </li>
            </ol>
            <p className="login-disclaimer">
              信号仅供参考，不构成交易指令。账户净资产数据以服务端提供内容为准。
            </p>
          </aside>
        </main>
        <footer className="login-footer">
          <span>A500 TRADER</span>
          <span>身份验证由已配置的服务提供</span>
        </footer>
      </div>
    );
  }

  return (
    <div className="shell">
      <header className="masthead">
        <a className="brand" href="#signals" aria-label="A500 Trader 首页">
          <span className="brand-mark">A</span>
          <span>
            A500 <b>TRADER</b>
          </span>
        </a>
        <nav aria-label="主要导航">
          <a href="#signals">信号</a>
          <a href="#account">账户</a>
          <a href="#imports">成交导入</a>
        </nav>
        <div className="connection">
          <span className="status-dot online" aria-hidden="true" />
          <span>{streamState}</span>
          <button className="text-button" onClick={disconnect}>
            断开
          </button>
        </div>
      </header>
      {!configurationReady && (
        <div className="config-note" role="status">
          需配置 <code>NEXT_PUBLIC_A500_OIDC_ISSUER</code> 后连接身份服务；API 使用同源{" "}
          <code>/api/v1</code>。
        </div>
      )}
      {error && (
        <div className="notice error" role="alert">
          <span>{error}</span>
          <button className="text-button" onClick={() => setError(null)}>
            关闭
          </button>
        </div>
      )}
      {notice && (
        <div className="notice" role="status">
          {notice}
        </div>
      )}
      <main className="layout">
        <section className="signal-section" id="signals" aria-labelledby="signals-title">
          <div className="section-heading">
            <div>
              <h1 id="signals-title">实时信号</h1>
              <p className="section-subtitle">
                已配置标的的当前策略状态 · 所有信号仅供参考，不构成交易指令
              </p>
            </div>
            <div className="updated">
              最后同步 <time>{formatTime(lastUpdated)}</time>
            </div>
          </div>
          <div className="table-wrap" role="region" aria-label="当前策略信号" tabIndex={0}>
            <table>
              <thead>
                <tr>
                  <th scope="col">标的</th>
                  <th scope="col">状态 / 方向</th>
                  <th scope="col">数量</th>
                  <th scope="col">策略说明</th>
                  <th scope="col">事件时间</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((signal) => (
                  <tr key={signal.symbol}>
                    <th scope="row">
                      <span className="symbol">{signal.symbol}</span>
                    </th>
                    <td>
                      <span
                        className={`state ${signal.status === "UNAVAILABLE" || signal.stale ? "unavailable" : ""}`}
                      >
                        {signalLabel(signal)}
                      </span>
                      {signal.stale && <span className="subline">数据过期</span>}
                    </td>
                    <td className="numeric">
                      {signal.stale || signal.status === "UNAVAILABLE"
                        ? "—"
                        : (signal.quantity ?? "—")}
                    </td>
                    <td className="reason">{signal.reasons.join("；") || "暂无说明"}</td>
                    <td className="timestamp">{formatTime(signal.bar_timestamp)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {signals.length === 0 && (
              <div className="empty-state">
                <strong>
                  {streamState === STREAM_NO_SYMBOLS
                    ? "当前没有可显示的标的"
                    : streamState === STREAM_DISCONNECTED
                      ? "正在读取当前信号"
                      : streamState}
                </strong>
                <span>
                  {streamState === STREAM_NO_SYMBOLS
                    ? "服务尚未返回已配置标的，将定时重新读取。"
                    : "暂不展示未验证的旧信号；连接恢复后重新读取当前状态。"}
                </span>
              </div>
            )}
            <div className="stream-foot">
              <span
                className={`status-dot ${streamState === STREAM_LIVE ? "online" : ""}`}
                aria-hidden="true"
              />{" "}
              {streamState} · 变更通过服务端事件更新
            </div>
          </div>
        </section>
        <aside className="side-rail">
          <section className="rail-section" id="account" aria-labelledby="account-title">
            <div className="rail-heading">
              <h2 id="account-title">账户净资产</h2>
              <button className="text-button" disabled={busy} onClick={() => void refreshNav()}>
                刷新
              </button>
            </div>
            <p className="rail-note">仅当前认证主体 · 人工录入，未经券商核验</p>
            {nav?.status === "available" && nav.nav !== null ? (
              <>
                <p className="nav-value">
                  <span>{nav.currency === "CNY" ? "¥" : ""}</span>
                  {nav.nav}
                </p>
                <p className="rail-note">计算时间 {formatTime(nav.calculated_at)}</p>
              </>
            ) : (
              <div className="rail-empty">
                {navError
                  ? `净资产读取失败：${navError}。请重试刷新。`
                  : nav
                    ? `暂不可估值：${nav.reason || "数据不完整"}`
                    : "正在读取账户净资产…"}
              </div>
            )}
          </section>
          <section className="rail-section" aria-labelledby="snapshot-title">
            <h2 id="snapshot-title">账户快照</h2>
            <p className="rail-note">
              完整替换六个字段；数量为整数，现金与成本为十进制字符串。时间必须包含时区。导入成交不会更新此快照。
            </p>
            <p className="rail-note">
              多标的示例：数量 {`{"510500":100,"159919":200}`}，成本{" "}
              {`{"510500":"5.20","159919":"4.00"}`}。保护底仓、可卖数量须与持仓标的一致。
            </p>
            <form
              className="snapshot-form"
              onSubmit={(event) => {
                event.preventDefault();
                void saveAccount();
              }}
            >
              {snapshotFields.map(([field, label]) => (
                <label key={field} className="snapshot-field">
                  <span>{label}</span>
                  {field === "as_of" || field === "available_cash" ? (
                    <input
                      value={draft[field]}
                      required
                      inputMode={field === "available_cash" ? "decimal" : "text"}
                      onChange={(event) => {
                        draftDirty.current = true;
                        setDraft({ ...draft, [field]: event.target.value });
                      }}
                    />
                  ) : (
                    <textarea
                      value={draft[field]}
                      rows={3}
                      spellCheck={false}
                      required
                      onChange={(event) => {
                        draftDirty.current = true;
                        setDraft({ ...draft, [field]: event.target.value });
                      }}
                    />
                  )}
                </label>
              ))}
              {snapshotError && (
                <p className="snapshot-error" role="alert">
                  {snapshotError}
                </p>
              )}
              <button
                className="button primary full"
                type="submit"
                disabled={snapshotBusy || !snapshotKnown || conflict}
              >
                保存账户快照
              </button>
              <button
                className="button secondary full"
                type="button"
                disabled={snapshotBusy}
                onClick={() => void readLatest()}
              >
                读取最新快照
              </button>
              {latest !== undefined && (
                <div className="snapshot-latest" role="status">
                  <p className="rail-note">
                    {latest ? `最新快照时间：${latest.snapshot.as_of}` : "服务端尚无快照。"}{" "}
                    当前草稿仍保留。
                  </p>
                  {latest && (
                    <pre aria-label="最新快照内容">{JSON.stringify(latest.snapshot, null, 2)}</pre>
                  )}
                  <button
                    className="button secondary full"
                    type="button"
                    onClick={() => {
                      setDraft(snapshotDraft(latest?.snapshot));
                      setEtag(latest?.etag ?? null);
                      setLatest(undefined);
                      setConflict(false);
                      setSnapshotError(null);
                      draftDirty.current = false;
                    }}
                  >
                    使用最新快照替换草稿
                  </button>
                  <button
                    className="text-button"
                    type="button"
                    onClick={() => {
                      setEtag(latest?.etag ?? null);
                      setLatest(undefined);
                      setConflict(false);
                      setSnapshotError(null);
                      setNotice("草稿已保留，下一次保存将基于刚读取的最新版本完整替换快照。");
                    }}
                  >
                    保留草稿并使用最新版本
                  </button>
                </div>
              )}
            </form>
          </section>
          <section
            className="rail-section import-section"
            id="imports"
            aria-labelledby="imports-title"
          >
            <h2 id="imports-title">成交文件导入</h2>
            <p className="rail-note">仅支持银河证券 CSV（GB18030）。文件上限 5 MiB、10,000 行。</p>
            <label className="file-label">
              选择成交文件
              <input
                type="file"
                accept=".csv,text/csv"
                disabled={busy}
                onChange={(event) => {
                  setFile(event.target.files?.[0] || null);
                  setPreview(null);
                }}
              />
            </label>
            <p className="file-name">{file?.name || "未选择文件"}</p>
            <button className="button primary" disabled={!file || busy} onClick={previewFile}>
              {busy ? "处理中…" : "预览文件"}
            </button>
            {preview && (
              <div className="preview" aria-live="polite">
                <h3>{preview.confirmed ? "已确认" : "导入预览"}</h3>
                <p className="rail-note">批次编号 {preview.batch_id}</p>
                <dl>
                  <div>
                    <dt>有效</dt>
                    <dd>{preview.valid}</dd>
                  </div>
                  <div>
                    <dt>新增</dt>
                    <dd>{preview.new}</dd>
                  </div>
                  <div>
                    <dt>重复</dt>
                    <dd>{preview.duplicates}</dd>
                  </div>
                  <div>
                    <dt>错误</dt>
                    <dd>{preview.errors}</dd>
                  </div>
                </dl>
                {preview.error_details?.length > 0 && (
                  <>
                    <p className="rail-note">
                      存在校验错误，后端不会允许确认。以下仅展示脱敏的逐行错误。
                    </p>
                    <ul className="import-errors" aria-label="导入逐行错误">
                      {preview.error_details.map((item, index) => (
                        <li key={index}>
                          第 {item.row ?? "—"} 行 · {item.field || "文件"} ·{" "}
                          {item.reason || item.code || "校验失败"}
                        </li>
                      ))}
                    </ul>
                  </>
                )}
                {!preview.confirmed && preview.errors === 0 && (
                  <button className="button secondary full" disabled={busy} onClick={confirmImport}>
                    确认导入
                  </button>
                )}
              </div>
            )}
          </section>
        </aside>
      </main>
      <footer className="page-footer">
        <span>信号与账户数据由 A500 服务提供。</span>
        <span>实时事件：{streamState}</span>
      </footer>
    </div>
  );
}
