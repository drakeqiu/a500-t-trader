"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  configurationReady,
  confirmPreview,
  discover,
  getNav,
  getSignals,
  pollDeviceToken,
  requestDeviceCode,
  streamSignals,
  uploadPreview,
  type AccountNav,
  type ImportPreview,
  type Signal,
} from "@/lib/a500-client";

type Device = {
  user_code: string;
  verification_uri: string;
  verification_uri_complete?: string;
  device_code: string;
  expires_in: number;
  interval?: number;
};

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

export default function Workspace() {
  const [token, setToken] = useState<string | null>(null);
  const [device, setDevice] = useState<Device | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [nav, setNav] = useState<AccountNav | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [streamState, setStreamState] = useState("尚未连接");
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const authAbort = useRef<AbortController | null>(null);

  const loadData = useCallback(async (accessToken: string) => {
    setError(null);
    const [signalResult, navResult] = await Promise.allSettled([
      getSignals(accessToken),
      getNav(accessToken),
    ]);
    if (signalResult.status === "fulfilled") {
      setSignals(signalResult.value);
      setLastUpdated(new Date().toISOString());
    } else
      setError(
        signalResult.reason instanceof Error ? signalResult.reason.message : "读取信号失败。",
      );
    if (navResult.status === "fulfilled") setNav(navResult.value);
    else if (String(navResult.reason).includes("404")) setNav(null);
  }, []);

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    let retry: number | undefined;
    let disposed = false;
    const connectStream = async () => {
      try {
        const current = await getSignals(token);
        setSignals(current);
        setLastUpdated(new Date().toISOString());
        setStreamState("实时订阅");
        await streamSignals(
          token,
          current.map((row) => row.symbol),
          controller.signal,
          (incoming) => {
            setSignals((existing) =>
              existing.map((row) => (row.symbol === incoming.symbol ? incoming : row)),
            );
            setLastUpdated(new Date().toISOString());
          },
        );
      } catch (reason) {
        if (!controller.signal.aborted) {
          setStreamState("连接中断");
          setError(reason instanceof Error ? reason.message : "实时订阅中断。");
          retry = window.setTimeout(() => {
            if (!disposed) void connectStream();
          }, 5000);
        }
      }
    };
    void connectStream();
    return () => {
      disposed = true;
      controller.abort();
      if (retry) window.clearTimeout(retry);
    };
  }, [token]);
  useEffect(() => () => authAbort.current?.abort(), []);

  async function connect() {
    setStreamState("连接中");
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const endpoints = await discover();
      const result = await requestDeviceCode(endpoints.device);
      setDevice(result);
      setNotice("请在另一台设备完成身份验证；访问令牌仅保存在当前页面内存中。");
      const controller = new AbortController();
      authAbort.current = controller;
      const accessToken = await pollDeviceToken(
        endpoints.token,
        result.device_code,
        result.interval || 5,
        controller.signal,
      );
      setToken(accessToken);
      setDevice(null);
      setNotice("数据服务已连接。");
      await loadData(accessToken);
    } catch (reason) {
      if (!(reason instanceof DOMException && reason.name === "AbortError"))
        setError(reason instanceof Error ? reason.message : "身份授权失败。");
      setDevice(null);
    } finally {
      setBusy(false);
    }
  }
  function disconnect() {
    authAbort.current?.abort();
    setToken(null);
    setDevice(null);
    setSignals([]);
    setNav(null);
    setPreview(null);
    setStreamState("尚未连接");
    setNotice("已断开；访问令牌已从页面内存清除。");
  }
  async function previewFile() {
    if (!token || !file) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      setPreview(await uploadPreview(token, file));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "文件预览失败。");
    } finally {
      setBusy(false);
    }
  }
  async function confirmImport() {
    if (!token || !preview) return;
    setBusy(true);
    setError(null);
    try {
      const result = await confirmPreview(token, preview.batch_id);
      setPreview(result);
      setNotice("导入已确认；成交将按后端定义标记为用户提供、未经系统核实。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "确认导入失败。");
    } finally {
      setBusy(false);
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
                需配置 <code>NEXT_PUBLIC_A500_API_BASE_URL</code> 与{" "}
                <code>NEXT_PUBLIC_A500_OIDC_ISSUER</code> 后连接数据服务。
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
                  <h2 id="auth-title">{device ? "在另一台设备完成授权" : "通过身份提供方继续"}</h2>
                  <p>
                    {device
                      ? "仅在官方验证页面输入此代码。"
                      : "我们会安全地将你转至已配置的身份提供方。"}
                  </p>
                </div>
                <span className={`auth-state ${busy ? "is-active" : ""}`}>
                  <span className="status-dot" aria-hidden="true" />
                  {busy ? "等待授权" : "尚未连接"}
                </span>
              </div>
              {device ? (
                <>
                  <div className="code-row">
                    <span>一次性设备代码</span>
                    <strong className="user-code">{device.user_code}</strong>
                  </div>
                  <a
                    className="button primary full auth-action"
                    href={device.verification_uri_complete || device.verification_uri}
                    target="_blank"
                    rel="noreferrer"
                  >
                    打开验证页面 <span aria-hidden="true">↗</span>
                  </a>
                  <button className="text-button auth-cancel" onClick={disconnect}>
                    取消授权
                  </button>
                </>
              ) : (
                <button
                  className="button primary full auth-action"
                  onClick={() => void connect()}
                  disabled={busy || !configurationReady}
                >
                  {busy ? "正在连接…" : "安全连接"}
                  {!busy && <span aria-hidden="true">→</span>}
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
                  <strong>确认设备代码</strong>
                  <p>在官方验证页面确认本次登录。</p>
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
          <span className={`status-dot ${token ? "online" : ""}`} aria-hidden="true" />
          <span>{token ? streamState : "未连接"}</span>
          {token ? (
            <button className="text-button" onClick={disconnect}>
              断开
            </button>
          ) : (
            <button
              className="text-button"
              onClick={connect}
              disabled={busy || !configurationReady}
            >
              {busy ? "连接中…" : "连接数据服务"}
            </button>
          )}
        </div>
      </header>
      {!configurationReady && (
        <div className="config-note" role="status">
          需配置 <code>NEXT_PUBLIC_A500_API_BASE_URL</code> 与{" "}
          <code>NEXT_PUBLIC_A500_OIDC_ISSUER</code> 后连接数据服务。
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
      {device && (
        <section className="device-panel" aria-live="polite">
          <div>
            <h2>完成身份验证</h2>
            <p>在受信任的身份提供方中输入此代码。不要将代码发送给他人。</p>
          </div>
          <strong className="user-code">{device.user_code}</strong>
          <a
            className="button secondary"
            href={device.verification_uri_complete || device.verification_uri}
            target="_blank"
            rel="noreferrer"
          >
            打开验证页面 ↗
          </a>
        </section>
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
                    <td className="numeric">{signal.quantity ?? "—"}</td>
                    <td className="reason">{signal.reasons.join("；") || "暂无说明"}</td>
                    <td className="timestamp">{formatTime(signal.bar_timestamp)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!token && (
              <div className="empty-state">
                <strong>连接后查看实时信号</strong>
                <span>信号状态来自现有服务；页面不会展示模拟数据。</span>
              </div>
            )}
            {token && signals.length === 0 && (
              <div className="empty-state">
                <strong>当前没有可显示的标的</strong>
                <span>服务尚未返回已配置标的。</span>
              </div>
            )}
            {token && (
              <div className="stream-foot">
                <span
                  className={`status-dot ${streamState === "实时订阅" ? "online" : ""}`}
                  aria-hidden="true"
                />{" "}
                {streamState} · 变更通过服务端事件更新
              </div>
            )}
          </div>
        </section>
        <aside className="side-rail">
          <section className="rail-section" id="account" aria-labelledby="account-title">
            <div className="rail-heading">
              <h2 id="account-title">账户净资产</h2>
              <button
                className="text-button"
                disabled={!token || busy}
                onClick={() => token && void loadData(token)}
              >
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
                {!token
                  ? "连接服务后读取当前账户。"
                  : nav
                    ? `暂不可估值：${nav.reason || "数据不完整"}`
                    : "尚无账户快照，或当前净资产不可用。"}
              </div>
            )}
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
                disabled={!token || busy}
                onChange={(event) => {
                  setFile(event.target.files?.[0] || null);
                  setPreview(null);
                }}
              />
            </label>
            <p className="file-name">{file?.name || "未选择文件"}</p>
            <button
              className="button primary"
              disabled={!token || !file || busy}
              onClick={previewFile}
            >
              {busy ? "处理中…" : "预览导入"}
            </button>
            {preview && (
              <div className="preview" aria-live="polite">
                <h3>{preview.confirmed ? "已确认" : "导入预览"}</h3>
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
                  <p className="rail-note">存在校验错误，后端不会允许确认。</p>
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
