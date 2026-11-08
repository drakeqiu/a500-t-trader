"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { AppShell } from "@/components/app-shell";
import { PageHeading } from "@/components/page-heading";
import {
  ApiError,
  getNav,
  getSnapshot,
  saveSnapshot,
  type AccountNav,
  type SnapshotResult,
} from "@/lib/a500-client";
import { beginAuthorization, configurationReady } from "@/lib/oidc";
import { clearAccessToken, getAccessToken, subscribeSession } from "@/lib/session";
import {
  parseSnapshotDraft,
  snapshotDraft,
  snapshotDraftWithPositions,
  snapshotPositions,
  type PositionDraft,
  type SnapshotDraft,
} from "@/lib/snapshot-draft";

const amount = (value: string | null | undefined, currency = "CNY") =>
  value === null || value === undefined
    ? "—"
    : `${currency === "CNY" ? "¥" : `${currency} `}${value}`;

function timeLabel(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
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

function reasonLabel(reason?: string) {
  const labels: Record<string, string> = {
    account_snapshot_missing: "尚无账户快照",
    account_snapshot_future: "账户快照时间晚于当前时间",
    account_snapshot_stale: "账户快照已超过 30 分钟有效期",
    quote_missing: "缺少必需持仓行情",
    quote_invalid_price: "持仓行情价格无效",
    quote_future: "持仓行情时间晚于当前时间",
    quote_stale: "持仓行情已超过 30 分钟有效期",
    market_data_unavailable: "行情服务暂不可用",
  };
  return reason ? `${labels[reason] ?? "净资产暂不可用"}（${reason}）` : "完整估值暂不可用";
}

export default function AssetsPage() {
  const token = useSyncExternalStore(subscribeSession, getAccessToken, () => null);
  return <AssetsDashboard key={token ?? "guest"} token={token} />;
}

function AssetsDashboard({ token }: { token: string | null }) {
  const [nav, setNav] = useState<AccountNav | null>(null);
  const [navError, setNavError] = useState<string | null>(null);
  const [navBusy, setNavBusy] = useState(false);
  const [snapshot, setSnapshot] = useState<SnapshotResult | null>(null);
  const [snapshotKnown, setSnapshotKnown] = useState(false);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [snapshotBusy, setSnapshotBusy] = useState(false);
  const [draft, setDraft] = useState<SnapshotDraft>(snapshotDraft());
  const [positions, setPositions] = useState<PositionDraft[]>([]);
  const [draftDirty, setDraftDirty] = useState(false);
  const [editing, setEditing] = useState(false);
  const [latest, setLatest] = useState<SnapshotResult | null | undefined>(undefined);
  const [conflict, setConflict] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [authBusy, setAuthBusy] = useState(false);
  const snapshotOperationRef = useRef(false);
  const snapshotRequestRef = useRef(0);
  const navRequestRef = useRef(0);
  function replaceDraft(value: SnapshotDraft) {
    setDraft(value);
    setPositions(snapshotPositions(value));
  }

  function updatePosition(index: number, field: keyof PositionDraft, value: string) {
    const next = positions.map((position, row) =>
      row === index ? { ...position, [field]: value } : position,
    );
    setPositions(next);
    setDraft((current) => snapshotDraftWithPositions(current, next));
    setDraftDirty(true);
    setSnapshotError(null);
  }

  function setPositionsAndDraft(next: PositionDraft[]) {
    setPositions(next);
    setDraft((current) => snapshotDraftWithPositions(current, next));
    setDraftDirty(true);
    setSnapshotError(null);
  }

  const refreshNav = useCallback(async (accessToken: string, signal?: AbortSignal) => {
    const request = ++navRequestRef.current;
    setNavBusy(true);
    setNavError(null);
    try {
      const result = await getNav(accessToken, signal);
      if (!signal?.aborted && request === navRequestRef.current) setNav(result);
    } catch (reason) {
      if (signal?.aborted || request !== navRequestRef.current) return;
      if (reason instanceof ApiError && reason.status === 401) clearAccessToken();
      setNavError(reason instanceof Error ? reason.message : "读取净资产失败。");
    } finally {
      if (!signal?.aborted && request === navRequestRef.current) setNavBusy(false);
    }
  }, []);

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    const load = async () => {
      await Promise.resolve();
      if (controller.signal.aborted) return;
      void refreshNav(token, controller.signal);
      const request = ++snapshotRequestRef.current;
      try {
        const result = await getSnapshot(token, controller.signal);
        if (controller.signal.aborted || request !== snapshotRequestRef.current) return;
        setSnapshot(result);
        setSnapshotKnown(true);
        replaceDraft(snapshotDraft(result?.snapshot));
      } catch (reason) {
        if (controller.signal.aborted || request !== snapshotRequestRef.current) return;
        if (reason instanceof ApiError && reason.status === 401) clearAccessToken();
        setSnapshotError(reason instanceof Error ? reason.message : "读取账户快照失败。");
      } finally {
        if (!controller.signal.aborted) setSnapshotKnown(true);
      }
    };
    void load();
    return () => controller.abort();
  }, [token, refreshNav]);

  function discardDraft() {
    const source = latest !== undefined ? latest : snapshot;
    setSnapshot(source);
    replaceDraft(snapshotDraft(source?.snapshot));
    setDraftDirty(false);
    setEditing(false);
    setLatest(undefined);
    setConflict(false);
    setConfirmDiscard(false);
    setSnapshotError(null);
  }

  async function connect() {
    if (!configurationReady) {
      setAuthError("身份服务尚未配置，请联系部署管理员。");
      return;
    }
    setAuthBusy(true);
    setAuthError(null);
    try {
      sessionStorage.setItem("a500.auth.return", "/assets");
      window.location.assign(await beginAuthorization());
    } catch (reason) {
      sessionStorage.removeItem("a500.auth.return");
      setAuthError(reason instanceof Error ? reason.message : "无法启动安全连接。");
      setAuthBusy(false);
    }
  }

  async function reloadSnapshot() {
    if (!token || snapshotOperationRef.current) return;
    snapshotOperationRef.current = true;
    const request = ++snapshotRequestRef.current;
    setSnapshotBusy(true);
    setSnapshotError(null);
    try {
      const result = await getSnapshot(token);
      if (request !== snapshotRequestRef.current) return;
      setLatest(result);
      if (!draftDirty) {
        setSnapshot(result);
        replaceDraft(snapshotDraft(result?.snapshot));
      }
      await refreshNav(token);
      setNotice("已读取服务端最新快照。当前草稿保持不变；请选择如何处理版本差异。");
    } catch (reason) {
      if (request !== snapshotRequestRef.current) return;
      if (reason instanceof ApiError && reason.status === 401) clearAccessToken();
      setSnapshotError(reason instanceof Error ? reason.message : "读取最新快照失败。");
    } finally {
      snapshotOperationRef.current = false;
      setSnapshotBusy(false);
    }
  }

  async function saveAccount(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || !snapshotKnown || conflict || snapshotOperationRef.current) return;
    snapshotOperationRef.current = true;
    ++snapshotRequestRef.current;
    setSnapshotBusy(true);
    setSnapshotError(null);
    setNotice(null);
    try {
      if (positions.some((position) => !position.symbol.trim())) {
        throw new Error("请为每行填写证券代码，或删除空白行。");
      }
      if (new Set(positions.map((position) => position.symbol.trim())).size !== positions.length) {
        throw new Error("证券代码不能重复。");
      }
      const result = await saveSnapshot(token, parseSnapshotDraft(draft), snapshot?.etag ?? null);
      setSnapshot(result);
      replaceDraft(snapshotDraft(result.snapshot));
      setDraftDirty(false);
      setEditing(false);
      setLatest(undefined);
      setConflict(false);
      setNotice("账户快照已保存。正在从服务端重新读取完整净资产估值。");
      await refreshNav(token);
    } catch (reason) {
      if (reason instanceof ApiError && (reason.status === 412 || reason.status === 428)) {
        setConflict(true);
        setSnapshotError(
          "快照版本已变化，草稿仍保留，未覆盖服务端数据。请读取最新快照后明确选择处理方式。",
        );
      } else {
        if (reason instanceof ApiError && reason.status === 401) clearAccessToken();
        const message = reason instanceof Error ? reason.message : "保存快照失败";
        setSnapshotError(`${message}。输入内容已保留。`);
      }
    } finally {
      snapshotOperationRef.current = false;
      setSnapshotBusy(false);
    }
  }

  function setField<K extends keyof SnapshotDraft>(field: K, value: string) {
    setDraft((current) => ({ ...current, [field]: value }));
    setDraftDirty(true);
    setSnapshotError(null);
  }

  const busy = snapshotBusy || !snapshotKnown;
  const hasSnapshot = snapshot !== null;

  return (
    <AppShell activePath="/assets" connected={Boolean(token)} onConnect={() => void connect()}>
      <section className="asset-page" aria-labelledby="assets-title">
        <PageHeading
          className="page-heading"
          title="账户资产"
          titleId="assets-title"
          description="查看服务端净资产估值与账户快照。快照为人工录入，未经券商核验。"
          descriptionClassName="page-intro"
        >
          {token && (
            <button
              className="refresh-button"
              type="button"
              disabled={navBusy}
              onClick={() => void refreshNav(token)}
            >
              {navBusy ? "正在更新…" : "刷新净资产"}
            </button>
          )}
        </PageHeading>

        {authError && (
          <div className="signal-error" role="alert">
            <p>{authError}</p>
          </div>
        )}
        {!token && (
          <section className="asset-state state-panel" aria-labelledby="assets-connect-title">
            <h2 id="assets-connect-title">安全连接后查看账户数据</h2>
            <p>账户快照和净资产仅对当前认证主体开放。登录后即可读取服务端最新状态。</p>
            <button
              className="refresh-button primary"
              type="button"
              disabled={authBusy}
              onClick={() => void connect()}
            >
              {authBusy ? "正在连接…" : "安全连接"}
            </button>
          </section>
        )}

        {token && (
          <>
            <section className="asset-section" aria-labelledby="nav-title">
              <div className="asset-section-heading">
                <div>
                  <h2 id="nav-title">当前净资产（NAV）</h2>
                  <p>
                    服务端根据账户快照的现金与非零持仓数量、有效行情计算
                    NAV；本页不复算。保护底仓、可卖数量和成本基础不参与估值。
                  </p>
                </div>
                <span
                  className={`asset-status ${navError || nav?.status === "unavailable" ? "is-unavailable" : nav?.status === "available" ? "is-available" : ""}`}
                  role="status"
                >
                  {navError
                    ? "读取失败"
                    : nav?.status === "available" && nav.nav !== null
                      ? "完整估值可用"
                      : nav?.status === "unavailable"
                        ? "暂不可估值"
                        : navBusy
                          ? "正在读取"
                          : "等待数据"}
                </span>
              </div>
              {navError ? (
                <div className="asset-state is-error" role="alert">
                  <p>净资产读取失败：{navError}</p>
                  <button
                    className="text-action"
                    type="button"
                    disabled={navBusy}
                    onClick={() => void refreshNav(token)}
                  >
                    重试读取
                  </button>
                </div>
              ) : nav?.status === "available" && nav.nav !== null ? (
                <>
                  <div className="nav-summary">
                    <p className="nav-total">
                      <span>{nav.currency === "CNY" ? "¥" : ""}</span>
                      {nav.nav}
                    </p>
                    <dl className="nav-metadata">
                      <div>
                        <dt>币种</dt>
                        <dd>{nav.currency ?? "—"}</dd>
                      </div>
                      <div>
                        <dt>可用现金</dt>
                        <dd>{amount(nav.available_cash, nav.currency)}</dd>
                      </div>
                      <div>
                        <dt>账户快照时间</dt>
                        <dd>{timeLabel(nav.account_snapshot_as_of)}</dd>
                      </div>
                      <div>
                        <dt>服务端计算时间</dt>
                        <dd>{timeLabel(nav.calculated_at)}</dd>
                      </div>
                    </dl>
                  </div>
                  {nav.holdings && nav.holdings.length > 0 && (
                    <div className="asset-table-scroll">
                      <table className="asset-table">
                        <caption>纳入完整估值的持仓（行情价格与市值为服务端数据）</caption>
                        <thead>
                          <tr>
                            <th scope="col">标的</th>
                            <th scope="col">持仓数量</th>
                            <th scope="col">行情价格</th>
                            <th scope="col">持仓市值</th>
                            <th scope="col">行情时间</th>
                          </tr>
                        </thead>
                        <tbody>
                          {nav.holdings.map((holding) => (
                            <tr key={holding.symbol}>
                              <th scope="row">{holding.symbol}</th>
                              <td>
                                {new Intl.NumberFormat("zh-CN", {
                                  maximumFractionDigits: 0,
                                }).format(holding.quantity)}
                              </td>
                              <td>{amount(holding.last_price, nav.currency)}</td>
                              <td>{amount(holding.market_value, nav.currency)}</td>
                              <td>{timeLabel(holding.as_of)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </>
              ) : nav?.status === "unavailable" ? (
                <div className="asset-state is-unavailable" role="status">
                  <strong>{reasonLabel(nav.reason)}</strong>
                  <p>服务端没有提供完整估值明细；此处不展示部分净值。</p>
                  {nav.account_snapshot_as_of && (
                    <p>账户快照时间：{timeLabel(nav.account_snapshot_as_of)}</p>
                  )}
                </div>
              ) : (
                <div className="asset-state" role="status">
                  {navBusy ? "正在读取账户净资产…" : "尚无可显示的净资产数据。"}
                </div>
              )}
              <p className="asset-disclaimer">
                {nav?.classification === "user-entered; not broker-verified"
                  ? "账户数据来源：用户录入，未经券商核验。"
                  : nav?.classification
                    ? `数据分类：${nav.classification}。`
                    : "账户数据来源：用户录入，未经券商核验。"}
                行情或快照缺失、超过 30 分钟有效期或时间异常时，完整 NAV
                不可用；金额保留服务端十进制精度。
              </p>
            </section>

            <section className="asset-section" aria-labelledby="snapshot-title">
              <div className="asset-section-heading">
                <div>
                  <h2 id="snapshot-title">账户快照</h2>
                  <p>快照是对当前状态的六字段完整替换；编辑前确认数量与金额来源。</p>
                </div>
                <div className="asset-actions">
                  {!editing && (
                    <button
                      className="refresh-button primary"
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        setEditing(true);
                        setSnapshotError(null);
                      }}
                    >
                      {hasSnapshot ? "编辑快照" : "创建快照"}
                    </button>
                  )}
                  {editing && (
                    <button
                      className="refresh-button"
                      type="button"
                      disabled={snapshotBusy}
                      onClick={() => {
                        if (draftDirty) {
                          setConfirmDiscard(true);
                        } else {
                          discardDraft();
                        }
                      }}
                    >
                      取消编辑
                    </button>
                  )}
                  <button
                    className="refresh-button"
                    type="button"
                    disabled={snapshotBusy}
                    onClick={() => void reloadSnapshot()}
                  >
                    {snapshotBusy ? "正在读取…" : "读取最新快照"}
                  </button>
                </div>
              </div>

              {snapshotError && (
                <div className="asset-state is-error" role="alert">
                  <p>{snapshotError}</p>
                </div>
              )}
              {notice && (
                <p className="asset-notice" role="status">
                  {notice}
                </p>
              )}

              {confirmDiscard && (
                <div className="asset-state" role="group" aria-label="确认放弃未保存更改">
                  <strong>放弃未保存的账户快照更改？</strong>
                  <p>当前输入尚未保存。</p>
                  <div className="asset-actions">
                    <button
                      className="refresh-button"
                      type="button"
                      onClick={() => setConfirmDiscard(false)}
                    >
                      继续编辑
                    </button>
                    <button className="refresh-button" type="button" onClick={discardDraft}>
                      放弃更改
                    </button>
                  </div>
                </div>
              )}
              {snapshotKnown && !hasSnapshot && !editing && (
                <div className="asset-state" role="status">
                  <strong>{snapshotError ? "账户快照读取失败" : "尚无账户快照"}</strong>
                  <p>
                    {snapshotError
                      ? "请重试读取最新快照；创建前确认您输入的是完整账户状态。"
                      : "保存完整快照后，服务端才能尝试计算净资产。"}
                  </p>
                </div>
              )}
              {!snapshotKnown && (
                <div className="asset-state" role="status">
                  正在读取账户快照…
                </div>
              )}

              {snapshot && !editing && <SnapshotView snapshot={snapshot.snapshot} />}
              {editing && (
                <form className="snapshot-editor" onSubmit={(event) => void saveAccount(event)}>
                  <p className="snapshot-editor-note">
                    按照你查看账户的实际情况填写。证券持仓按行录入，不用编写
                    JSON；保存后会完整替换当前快照。
                  </p>
                  <div className="snapshot-fields">
                    <label className="snapshot-field">
                      <span>快照时间</span>
                      <input
                        aria-label="快照时间"
                        aria-describedby="as_of-hint"
                        value={draft.as_of}
                        required
                        onChange={(event) => setField("as_of", event.target.value)}
                      />
                      <small id="as_of-hint">
                        记录这份余额和持仓对应的时间，必须包含时区。例：2026-11-06T15:00:00+08:00
                      </small>
                      <button
                        className="text-action snapshot-time-action"
                        type="button"
                        onClick={() => setField("as_of", new Date().toISOString())}
                      >
                        填入当前时间（UTC）
                      </button>
                    </label>
                    <label className="snapshot-field">
                      <span>可用现金（元）</span>
                      <input
                        aria-label="可用现金"
                        aria-describedby="available_cash-hint"
                        value={draft.available_cash}
                        required
                        inputMode="decimal"
                        onChange={(event) => setField("available_cash", event.target.value)}
                      />
                      <small id="available_cash-hint">
                        填账户当前可用于交易的现金，不含持仓市值。例：5000.00
                      </small>
                    </label>
                  </div>
                  <section className="position-editor" aria-labelledby="positions-title">
                    <div className="position-editor-heading">
                      <div>
                        <h3 id="positions-title">证券持仓</h3>
                        <p>每只证券一行。没有持仓时可以留空；保护底仓和可卖数量按账户实际填写。</p>
                      </div>
                      <button
                        className="refresh-button"
                        type="button"
                        onClick={() =>
                          setPositionsAndDraft([
                            ...positions,
                            {
                              symbol: "",
                              holdings: "0",
                              protected_floor: "0",
                              sellable: "0",
                              cost_basis: "",
                            },
                          ])
                        }
                      >
                        添加证券
                      </button>
                    </div>
                    {positions.length === 0 ? (
                      <p className="position-empty">
                        当前没有持仓行。若账户持有证券，请添加一行填写。
                      </p>
                    ) : (
                      <div className="position-list">
                        {positions.map((position, index) => (
                          <fieldset className="position-row" key={index}>
                            <legend>证券 {index + 1}</legend>
                            <label>
                              <span>证券代码</span>
                              <input
                                aria-label={`第 ${index + 1} 行证券代码`}
                                value={position.symbol}
                                placeholder="例如 510300"
                                required
                                onChange={(event) =>
                                  updatePosition(index, "symbol", event.target.value)
                                }
                              />
                            </label>
                            <label>
                              <span>持有总份额</span>
                              <input
                                aria-label={`第 ${index + 1} 行持有总份额`}
                                inputMode="numeric"
                                value={position.holdings}
                                required
                                aria-describedby="position-holdings-hint"
                                onChange={(event) =>
                                  updatePosition(index, "holdings", event.target.value)
                                }
                              />
                            </label>
                            <label>
                              <span>保护底仓</span>
                              <input
                                aria-label={`第 ${index + 1} 行保护底仓`}
                                inputMode="numeric"
                                value={position.protected_floor}
                                required
                                aria-describedby="position-floor-hint"
                                onChange={(event) =>
                                  updatePosition(index, "protected_floor", event.target.value)
                                }
                              />
                            </label>
                            <label>
                              <span>当前可卖</span>
                              <input
                                aria-label={`第 ${index + 1} 行当前可卖`}
                                inputMode="numeric"
                                value={position.sellable}
                                required
                                aria-describedby="position-sellable-hint"
                                onChange={(event) =>
                                  updatePosition(index, "sellable", event.target.value)
                                }
                              />
                            </label>
                            <label>
                              <span>成本基础（元）</span>
                              <input
                                aria-label={`第 ${index + 1} 行成本基础`}
                                inputMode="decimal"
                                value={position.cost_basis}
                                placeholder="可留空"
                                aria-describedby="position-cost-hint"
                                onChange={(event) =>
                                  updatePosition(index, "cost_basis", event.target.value)
                                }
                              />
                            </label>
                            <button
                              className="text-action position-remove"
                              type="button"
                              onClick={() =>
                                setPositionsAndDraft(positions.filter((_, row) => row !== index))
                              }
                            >
                              删除此行
                            </button>
                          </fieldset>
                        ))}
                      </div>
                    )}
                    <div className="position-hints">
                      <small id="position-holdings-hint">账户中该证券的持仓总份额。</small>
                      <small id="position-floor-hint">
                        其中不打算卖出的保留份额，不能大于总持仓。
                      </small>
                      <small id="position-sellable-hint">
                        当前实际可卖份额，不能超过持仓减保护底仓。
                      </small>
                      <small id="position-cost-hint">
                        成本基础金额；暂不清楚时可留空。例：3.8500
                      </small>
                    </div>
                  </section>
                  {conflict && (
                    <div className="asset-conflict" role="group" aria-label="处理快照版本冲突">
                      <p>草稿内容仍保留。不会自动覆盖其他版本。</p>
                      {latest !== undefined && (
                        <>
                          <button
                            className="refresh-button"
                            type="button"
                            disabled={snapshotBusy}
                            onClick={() => {
                              setSnapshot(latest);
                              replaceDraft(snapshotDraft(latest?.snapshot));
                              setDraftDirty(false);
                              setEditing(false);
                              setConflict(false);
                              setLatest(undefined);
                              setSnapshotError(null);
                            }}
                          >
                            使用最新快照替换草稿
                          </button>
                          <button
                            className="refresh-button"
                            type="button"
                            onClick={() => {
                              setSnapshot(latest);
                              setConflict(false);
                              setLatest(undefined);
                              setSnapshotError(null);
                              setNotice("草稿已保留；下次保存将以最新快照版本为条件进行完整替换。");
                            }}
                          >
                            保留草稿并使用最新版本
                          </button>
                        </>
                      )}
                    </div>
                  )}
                  <div className="snapshot-submit-row">
                    <button
                      className="refresh-button primary"
                      type="submit"
                      disabled={busy || conflict}
                    >
                      {snapshotBusy ? "正在保存…" : hasSnapshot ? "保存账户快照" : "创建账户快照"}
                    </button>
                    <span>
                      {snapshotBusy
                        ? "正在与服务端同步…"
                        : draftDirty
                          ? "有未保存的更改"
                          : "尚未修改草稿"}
                    </span>
                  </div>
                </form>
              )}
            </section>
          </>
        )}
      </section>
    </AppShell>
  );
}

function SnapshotView({ snapshot }: { snapshot: NonNullable<SnapshotResult>["snapshot"] }) {
  const entries = [
    ["快照时间（含时区）", snapshot.as_of],
    ["可用现金", amount(snapshot.available_cash)],
    ["持仓数量", snapshot.holdings],
    ["保护底仓", snapshot.protected_floor],
    ["可卖数量", snapshot.sellable],
    ["成本基础", snapshot.cost_basis],
  ] as const;

  return (
    <div className="snapshot-readonly">
      <p className="snapshot-origin">当前已保存 · 人工录入，未经券商核验</p>
      <dl>
        {entries.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>
              {typeof value === "string" ? (
                label === "快照时间（含时区）" ? (
                  <time dateTime={snapshot.as_of}>{timeLabel(value)}</time>
                ) : (
                  value
                )
              ) : (
                <pre>{JSON.stringify(value, null, 2)}</pre>
              )}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
