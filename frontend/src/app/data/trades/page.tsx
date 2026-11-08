"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  ApiError,
  confirmPreview,
  getPreview,
  uploadPreview,
  type ImportPreview,
} from "@/lib/a500-client";
import { AppShell } from "@/components/app-shell";
import { PageHeading } from "@/components/page-heading";
import { beginAuthorization, configurationReady } from "@/lib/oidc";
import { clearAccessToken, getAccessToken, subscribeSession } from "@/lib/session";

const batchKey = "a500.import-batch-id";
const modeKey = "a500.import-mode";
const maxBytes = 5 * 1024 * 1024;
const useHydrated = () => {
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setHydrated(true), 0);
    return () => window.clearTimeout(timer);
  }, []);
  return hydrated;
};

function errorMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 401) return "授权已失效，请重新安全连接。";
  if (error instanceof ApiError && error.status === 404)
    return "该批次不存在、已过期或不属于当前账户。请重新选择文件。";
  if (error instanceof ApiError && error.status === 409)
    return "批次校验失败或与已有成交冲突；未能完成导入。请重新预览文件。";
  if (error instanceof ApiError && error.status === 413)
    return "文件超过服务端限制（5 MiB 或 10,000 条记录）。请拆分为较小文件后重试。";
  if (error instanceof ApiError && error.status === 422)
    return "文件不是受支持的银河证券 GB18030 CSV，或表头格式不符。请导出原始 CSV 后重试。";
  if (error instanceof ApiError && error.status === 429) return "请求过于频繁，请稍等片刻后重试。";
  if (error instanceof ApiError && error.status >= 500)
    return "服务暂时无法处理请求。请稍后重试；确认请求失败时先查询批次状态。";
  return error instanceof TypeError
    ? "网络连接中断，服务端可能尚未收到请求。请检查网络后重试。"
    : error instanceof Error
      ? error.message
      : "请求失败，请稍后重试。";
}
function isImportConfirmed(value: unknown): boolean {
  return Boolean(
    value && typeof value === "object" && "confirmed" in value && value.confirmed === true,
  );
}

function fileError(file: File) {
  if (!file.size) return "所选文件为空。请选择包含成交记录的 CSV 文件。";
  if (!file.name.toLocaleLowerCase().endsWith(".csv"))
    return "文件扩展名不是 .csv。请选择银河证券导出的 CSV 文件。";
  if (file.size > maxBytes) return "文件超过 5 MiB 上限。请选择较小的银河证券 CSV 文件。";
  return "";
}

export default function TradeImportPage() {
  const token = useSyncExternalStore(subscribeSession, getAccessToken, () => null);
  const hydrated = useHydrated();
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<"current" | "historical">("current");
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [duplicatesAcknowledged, setDuplicatesAcknowledged] = useState(false);
  const [error, setError] = useState("");
  const [restored, setRestored] = useState(false);
  const inFlight = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    if (!hydrated) return;
    const timer = window.setTimeout(() => {
      const savedMode = window.localStorage.getItem(modeKey);
      if (savedMode === "current" || savedMode === "historical") setMode(savedMode);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [hydrated]);
  useEffect(() => {
    if (!hydrated || !token) return;
    const batchId = window.localStorage.getItem(batchKey);
    if (!batchId) return;
    const request = new AbortController();
    controller.current = request;
    const busyTimer = window.setTimeout(() => setBusy(true), 0);
    void getPreview(token, batchId, request.signal)
      .then((result) => {
        setError("");
        setPreview(result);
        setRestored(true);
        setNotice("已恢复此账户最近保存的成交批次。");
      })
      .catch((reason: unknown) => {
        if (request.signal.aborted) return;
        if (reason instanceof ApiError && reason.status === 401) clearAccessToken();
        window.localStorage.removeItem(batchKey);
        setPreview(null);
        setRestored(false);
        setError(
          reason instanceof ApiError && reason.status === 404
            ? "保存的导入批次已过期或不可用，请重新选择文件。"
            : errorMessage(reason),
        );
      })
      .finally(() => {
        if (!request.signal.aborted) setBusy(false);
      });
    return () => {
      window.clearTimeout(busyTimer);
      request.abort();
    };
  }, [hydrated, token]);

  const connect = useCallback(() => {
    if (!configurationReady) {
      setError("缺少身份服务配置，暂时无法安全连接。");
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
        setError(errorMessage(reason));
      });
  }, []);

  const chooseFile = (selected: File | null) => {
    setFile(selected);
    setPreview(null);
    setRestored(false);
    setDuplicatesAcknowledged(false);
    setError("");
    setNotice("");
    if (selected) window.localStorage.removeItem(batchKey);
  };
  const chooseMode = (selected: "current" | "historical") => {
    setMode(selected);
    window.localStorage.setItem(modeKey, selected);
  };

  const previewFile = async () => {
    if (!token || !file || inFlight.current) return;
    const invalid = fileError(file);
    if (invalid) {
      setError(invalid);
      return;
    }
    inFlight.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    const request = new AbortController();
    controller.current = request;
    try {
      const result = await uploadPreview(token, file, request.signal);
      window.localStorage.setItem(batchKey, result.batch_id);
      setPreview(result);
      setDuplicatesAcknowledged(false);
      setRestored(false);
    } catch (reason) {
      if (request.signal.aborted) {
        setNotice(
          "已取消上传。没有收到预览结果；如服务端已接收文件，该临时批次会在 15 分钟后过期。你可以重新上传。",
        );
      } else {
        if (reason instanceof ApiError && reason.status === 401) clearAccessToken();
        setError(errorMessage(reason));
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
      if (controller.current === request) controller.current = null;
    }
  };

  const cancelUpload = () => {
    if (busy && !preview) controller.current?.abort();
  };

  const refreshBatch = async () => {
    if (!token || !preview || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    const request = new AbortController();
    controller.current = request;
    try {
      const result = await getPreview(token, preview.batch_id, request.signal);
      setPreview(result);
      setNotice(
        isImportConfirmed(result.confirmed)
          ? "服务端已确认导入，以下为已核实的批次结果。"
          : "服务端批次仍待确认；可再次确认，同一批次不会重复写入。",
      );
    } catch (reason) {
      if (!request.signal.aborted) {
        if (reason instanceof ApiError && reason.status === 401) clearAccessToken();
        setError(`无法查询批次状态。${errorMessage(reason)} 请保持此批次，网络恢复后再次查询。`);
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
      if (controller.current === request) controller.current = null;
    }
  };

  const confirmImport = async () => {
    if (
      !token ||
      !preview ||
      isImportConfirmed(preview.confirmed) ||
      preview.errors > 0 ||
      (preview.duplicates > 0 && !duplicatesAcknowledged) ||
      inFlight.current
    )
      return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    const request = new AbortController();
    controller.current = request;
    try {
      const result = await confirmPreview(token, preview.batch_id, request.signal);
      setPreview(result);
      setNotice(
        isImportConfirmed(result.confirmed)
          ? "导入已确认。成交记录已写入导入台账，不会更新账户快照。"
          : "收到确认响应，但服务端未报告已确认；请查询批次状态。",
      );
    } catch (reason) {
      if (!request.signal.aborted && !(reason instanceof ApiError && reason.status === 401)) {
        try {
          const current = await getPreview(token, preview.batch_id);
          setPreview(current);
          if (isImportConfirmed(current.confirmed)) {
            setNotice("确认响应中断，但服务端批次已核实为完成。以下为已确认结果。");
          } else {
            setError(
              "确认请求未返回成功，服务端批次仍待确认。未显示为成功；检查网络后可再次确认，同一批次不会重复写入。",
            );
          }
        } catch {
          setError(
            "确认请求的响应中断，无法确认服务端最终状态。不要重新上传文件；网络恢复后查询批次状态，再按结果继续。",
          );
        }
      } else if (!request.signal.aborted) {
        clearAccessToken();
        setError(errorMessage(reason));
      } else {
        setError(
          "确认请求已中断，服务端最终状态尚未核实。网络恢复后查询批次状态；不要重新上传文件。",
        );
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
      if (controller.current === request) controller.current = null;
    }
  };

  const confirmed =
    preview?.confirmed && typeof preview.confirmed === "object"
      ? (preview.confirmed as {
          inserted_ids?: number[];
          new?: number;
          duplicates?: number;
          confirmed?: boolean;
        })
      : null;
  const isConfirmed = isImportConfirmed(preview?.confirmed);

  return (
    <AppShell activePath="/data/trades" connected={Boolean(token)} onConnect={connect}>
      <div className="trade-import-page">
        <PageHeading
          as="header"
          className="trade-import-heading"
          title="成交导入"
          description="核对文件解析结果后，再由你确认写入成交台账。"
        >
          <Link className="trade-import-secondary" href="/assets">
            查看资产快照
          </Link>
        </PageHeading>

        {!token ? (
          <section className="trade-import-panel" aria-labelledby="trade-import-auth-title">
            <h2 id="trade-import-auth-title">连接账户以开始导入</h2>
            <p>成交批次按登录主体隔离。导入不会改写账户快照，也不会执行券商交易。</p>
            <button className="trade-import-primary" disabled={!hydrated} onClick={connect}>
              安全连接
            </button>
          </section>
        ) : (
          <>
            <ol className="trade-import-steps" aria-label="成交导入流程">
              <li
                aria-current={!preview ? "step" : undefined}
                className={!preview ? "is-current" : "is-done"}
              >
                <span>选择与上传</span>
              </li>
              <li
                aria-current={preview && !isConfirmed ? "step" : undefined}
                className={preview ? (isConfirmed ? "is-done" : "is-current") : ""}
              >
                <span>检查预览</span>
              </li>
              <li
                aria-current={isConfirmed ? "step" : undefined}
                className={isConfirmed ? "is-done" : ""}
              >
                <span>确认导入</span>
              </li>
            </ol>

            <section className="trade-import-panel" aria-labelledby="trade-import-file-title">
              <div className="trade-import-section-head">
                <div>
                  <h2 id="trade-import-file-title">选择成交文件</h2>
                  <p>仅支持银河证券 GB18030 CSV；最多 5 MiB、10,000 条记录。</p>
                </div>
                <span className="trade-import-format">GB18030 · CSV</span>
              </div>
              <fieldset
                className="trade-import-mode"
                disabled={busy || isConfirmed || Boolean(preview)}
              >
                <legend>成交类型</legend>
                <label>
                  <input
                    type="radio"
                    name="trade-mode"
                    value="current"
                    checked={mode === "current"}
                    onChange={() => chooseMode("current")}
                  />{" "}
                  当前成交
                </label>
                <label>
                  <input
                    type="radio"
                    name="trade-mode"
                    value="historical"
                    checked={mode === "historical"}
                    onChange={() => chooseMode("historical")}
                  />{" "}
                  历史成交
                </label>
                <p>
                  此选择用于标记本次导入意图。后端从文件中的成交日期读取时间，不会根据此选择改写记录。
                </p>
              </fieldset>
              <details className="trade-import-format-details">
                <summary>查看文件字段要求</summary>
                <p>
                  必须使用银河证券导出的 GB18030 CSV，首行需与原始表头完全一致（共 20
                  列，最后一列为空）。
                </p>
                <p>
                  解析成交日期、时间、证券代码、名称、买卖操作、数量、均价、金额、合同编号、成交编号及手续费、印花税、其他杂费、发生金额；余额、市场、股东账户、交收日期和证券中文全称等字段用于匹配原始表头，不写入成交记录。
                </p>
                <p>
                  表头字段：
                  <code>
                    成交日期，成交时间，证券代码，证券名称，操作，成交数量，成交均价，成交金额，合同编号，成交编号，手续费，印花税，其他杂费，发生金额，资金余额，交易市场，股东帐户，交收日期，证券中文全称，〔空列〕
                  </code>
                </p>
              </details>
              <label className="trade-import-file">
                <span>上传 CSV 文件</span>
                <input
                  ref={fileInput}
                  aria-label="选择成交文件"
                  type="file"
                  accept=".csv,text/csv"
                  disabled={busy || isConfirmed}
                  onChange={(event) => chooseFile(event.target.files?.[0] ?? null)}
                />
              </label>
              {file && (
                <p className="trade-import-filename">
                  {file.name} · {(file.size / 1024 / 1024).toFixed(2)} MiB
                </p>
              )}
              <div className="trade-import-actions">
                <button
                  className="trade-import-primary"
                  disabled={!file || busy || !hydrated || Boolean(fileError(file))}
                  onClick={previewFile}
                >
                  {busy ? "正在上传并解析…" : "上传并预览"}
                </button>
                {busy && !preview && (
                  <button className="trade-import-secondary" onClick={cancelUpload}>
                    取消上传
                  </button>
                )}
                {file && !busy && (
                  <button
                    className="trade-import-secondary"
                    onClick={() => {
                      chooseFile(null);
                      if (fileInput.current) fileInput.current.value = "";
                    }}
                  >
                    清除文件
                  </button>
                )}
              </div>
              {file && fileError(file) && (
                <p className="trade-import-error" role="alert">
                  {fileError(file)}
                </p>
              )}
              {busy && (
                <p className="trade-import-progress" role="status">
                  正在由服务端处理文件；API 不提供百分比进度。
                </p>
              )}
              <p className="trade-import-help">
                预览仅显示后端提供的汇总和脱敏错误，不公开解析后的成交字段。
              </p>
            </section>

            {error && (
              <p className="trade-import-error" role="alert">
                {error}
              </p>
            )}
            {notice && (
              <p className="trade-import-notice" role="status">
                {notice}
              </p>
            )}

            {preview && (
              <section
                className="trade-import-panel trade-import-review"
                aria-labelledby="trade-import-review-title"
                aria-live="polite"
              >
                <div className="trade-import-section-head">
                  <div>
                    <h2 id="trade-import-review-title">
                      {isConfirmed ? "已确认" : "检查导入预览"}
                    </h2>
                    <p>
                      {restored
                        ? "从已保存批次恢复"
                        : "以下为服务端校验汇总；确认前请检查每项结果。"}
                    </p>
                  </div>
                  <span
                    className={`trade-import-status${isConfirmed ? " is-success" : preview.errors ? " is-error" : ""}`}
                  >
                    {isConfirmed ? "已写入" : preview.errors ? "存在错误" : "待确认"}
                  </span>
                </div>
                <dl className="trade-import-summary">
                  <div>
                    <dt>有效记录</dt>
                    <dd>{preview.valid}</dd>
                  </div>
                  <div>
                    <dt>预计新增</dt>
                    <dd>{preview.new}</dd>
                  </div>
                  <div>
                    <dt>重复记录</dt>
                    <dd>{preview.duplicates}</dd>
                  </div>
                  <div>
                    <dt>校验错误</dt>
                    <dd>{preview.errors}</dd>
                  </div>
                </dl>
                <p className="trade-import-batch">
                  批次编号 <code>{preview.batch_id}</code>
                </p>
                {!isConfirmed && (
                  <p className="trade-import-retention">
                    预览批次由服务端保留 15 分钟；此页面会尝试恢复尚未过期的批次。
                  </p>
                )}
                {preview.error_details.length > 0 && (
                  <div className="trade-import-validation">
                    <h3>需要修正的记录</h3>
                    <p>存在校验错误时，后端禁止确认导入。错误定位仅展示 API 返回的行号和字段。</p>
                    <ul aria-label="导入逐行错误">
                      {preview.error_details.map((item, index) => (
                        <li key={`${item.row}-${item.field}-${index}`}>
                          <strong>第 {item.row ?? "—"} 行</strong>
                          <span>{item.field || "文件"}</span>
                          <span>{item.reason || item.code || "校验失败"}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {isConfirmed && (
                  <div className="trade-import-result">
                    <h3>导入结果</h3>
                    <p>
                      已确认 {confirmed?.new ?? preview.new} 条新增记录；
                      {confirmed?.duplicates ?? preview.duplicates} 条重复记录未重复写入。
                    </p>
                    {confirmed?.inserted_ids && confirmed.inserted_ids.length > 0 && (
                      <p>新增记录编号：{confirmed.inserted_ids.join("、")}</p>
                    )}
                  </div>
                )}
                {!isConfirmed && preview.errors === 0 && (
                  <div className="trade-import-confirm">
                    <div>
                      <h3>确认写入成交台账</h3>
                      <p>确认后无法从此页面撤销；账户快照不会变化。</p>
                      {preview.duplicates > 0 && (
                        <label className="trade-import-duplicate-ack">
                          <input
                            type="checkbox"
                            checked={duplicatesAcknowledged}
                            onChange={(event) => setDuplicatesAcknowledged(event.target.checked)}
                          />
                          我已确认 {preview.duplicates} 条重复记录不会再次写入
                        </label>
                      )}
                    </div>
                    <button
                      className="trade-import-primary"
                      disabled={
                        busy || !hydrated || (preview.duplicates > 0 && !duplicatesAcknowledged)
                      }
                      onClick={confirmImport}
                    >
                      {busy ? "确认处理中…" : "确认导入"}
                    </button>
                  </div>
                )}
                {!isConfirmed && preview.errors > 0 && (
                  <p className="trade-import-blocked">
                    存在校验错误，确认操作不可用。请修正 CSV 后重新上传；当前批次不会写入。
                  </p>
                )}
                {!isConfirmed && (
                  <div className="trade-import-actions">
                    <button
                      className="trade-import-secondary"
                      disabled={busy}
                      onClick={refreshBatch}
                    >
                      重新查询批次状态
                    </button>
                    <button
                      className="trade-import-secondary"
                      disabled={busy}
                      onClick={() => {
                        chooseFile(null);
                        if (fileInput.current) fileInput.current.value = "";
                      }}
                    >
                      放弃此批次并选择新文件
                    </button>
                  </div>
                )}
                {isConfirmed && (
                  <Link className="trade-import-secondary" href="/assets">
                    前往资产快照
                  </Link>
                )}
              </section>
            )}
          </>
        )}
      </div>
    </AppShell>
  );
}
