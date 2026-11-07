export type Signal = {
  schema_version: number;
  symbol: string;
  bar_timestamp: string | null;
  strategy_identity: string;
  rule_version: string;
  parameters: Record<string, unknown>;
  parameter_fingerprint: string;
  status: string;
  direction: string | null;
  quantity: number | null;
  reasons: string[];
  stale: boolean;
  execution_eligible: boolean;
};
export type AccountNav = {
  status: "available" | "unavailable";
  currency?: string;
  nav: string | null;
  available_cash?: string | null;
  account_snapshot_as_of?: string | null;
  calculated_at?: string | null;
  classification?: string;
  reason?: string;
};
export type AccountSnapshot = {
  as_of: string;
  holdings: Record<string, number>;
  protected_floor: Record<string, number>;
  sellable: Record<string, number>;
  cost_basis: Record<string, string>;
  available_cash: string;
};
export type SnapshotResult = { snapshot: AccountSnapshot; etag: string };
export type ImportPreview = {
  batch_id: string;
  valid: number;
  new: number;
  duplicates: number;
  errors: number;
  error_details: Array<{ row?: number; field?: string; reason?: string; code?: string }>;
  confirmed?: unknown;
};
export type SignalEvent = { event: string; id?: string; data: unknown };
type SignalResponse = { signals: Signal[]; strategy: string; parameter_fingerprint: string };
const apiOrigin = "/api";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
async function responseJson<T>(response: Response): Promise<T> {
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const details = Array.isArray(body?.errors)
      ? body.errors
          .map((item: { path: string; code: string }) => `${item.path}: ${item.code}`)
          .join("；")
      : "";
    throw new ApiError(
      response.status,
      response.status === 401
        ? "授权已失效，请重新安全连接。"
        : details ||
            (typeof body?.detail === "string" ? body.detail : body?.title) ||
            `服务请求失败（${response.status}）`,
    );
  }
  return body as T;
}
export function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
  const { promise, resolve, reject } = Promise.withResolvers<void>();
  const abort = () => {
    clearTimeout(timer);
    reject(new DOMException("Aborted", "AbortError"));
  };
  const timer = setTimeout(() => {
    signal.removeEventListener("abort", abort);
    resolve();
  }, milliseconds);
  signal.addEventListener("abort", abort, { once: true });
  return promise;
}
function request(path: string, token: string, init?: RequestInit) {
  return fetch(`${apiOrigin}${path}`, {
    ...init,
    cache: "no-store",
    headers: { Authorization: `Bearer ${token}`, ...init?.headers },
  });
}
async function api<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  return responseJson<T>(await request(path, token, init));
}
export async function getSignals(token: string, signal?: AbortSignal): Promise<Signal[]> {
  return (await api<SignalResponse>("/v1/signals", token, { signal })).signals;
}
export async function getNav(token: string, signal?: AbortSignal): Promise<AccountNav> {
  const response = await request("/v1/account-nav", token, { signal });
  // The account API returns its explicit unavailable envelope with 404 when no snapshot exists.
  if (response.status === 404) return response.json() as Promise<AccountNav>;
  return responseJson<AccountNav>(response);
}
export async function getSnapshot(
  token: string,
  signal?: AbortSignal,
): Promise<SnapshotResult | null> {
  const response = await request("/v1/account-snapshot", token, { signal });
  if (response.status === 404) return null;
  const snapshot = await responseJson<AccountSnapshot>(response);
  const etag = response.headers.get("ETag");
  if (!etag) throw new Error("快照缺少 ETag，无法安全保存；请检查 API 代理或 CORS 配置。");
  return { snapshot, etag };
}
export async function saveSnapshot(
  token: string,
  snapshot: AccountSnapshot,
  etag: string | null,
  signal?: AbortSignal,
): Promise<SnapshotResult> {
  const response = await request("/v1/account-snapshot", token, {
    method: "PUT",
    signal,
    headers: {
      "Content-Type": "application/json",
      ...(etag ? { "If-Match": etag } : { "If-None-Match": "*" }),
    },
    body: JSON.stringify(snapshot),
  });
  const saved = await responseJson<AccountSnapshot>(response);
  const nextEtag = response.headers.get("ETag");
  if (!nextEtag) throw new Error("快照已保存但未返回 ETag；请读取最新快照后再保存。");
  return { snapshot: saved, etag: nextEtag };
}
export function uploadPreview(token: string, file: File, signal?: AbortSignal) {
  const body = new FormData();
  body.append("file", file);
  return api<ImportPreview>("/v1/trade-imports/previews", token, { method: "POST", body, signal });
}
export function getPreview(token: string, batchId: string, signal?: AbortSignal) {
  return api<ImportPreview>(`/v1/trade-imports/previews/${encodeURIComponent(batchId)}`, token, {
    signal,
  });
}
export async function confirmPreview(token: string, batchId: string, signal?: AbortSignal) {
  // Confirm returns a ledger summary, not a preview envelope. Re-read the durable batch.
  await api<unknown>(`/v1/trade-imports/previews/${encodeURIComponent(batchId)}/confirm`, token, {
    method: "POST",
    signal,
  });
  return getPreview(token, batchId, signal);
}
export function parseSignalEvent(block: string): SignalEvent | null {
  let event = "message";
  let id: string | undefined;
  const data: string[] = [];
  for (const line of block.split(/\r\n|\r|\n/)) {
    if (line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const raw = separator < 0 ? "" : line.slice(separator + 1);
    const value = raw.startsWith(" ") ? raw.slice(1) : raw;
    if (field === "event") event = value;
    if (field === "id" && !value.includes("\0")) id = value;
    if (field === "data") data.push(value);
  }
  if (!data.length) return id === undefined ? null : { event, id, data: null };
  try {
    return { event, id, data: JSON.parse(data.join("\n")) };
  } catch {
    return null;
  }
}
export async function streamSignals(
  token: string,
  symbols: string[],
  signal: AbortSignal,
  onEvent: (event: SignalEvent) => void,
  cursor?: string,
) {
  if (symbols.length === 0) return;
  const query = new URLSearchParams();
  symbols.forEach((symbol) => query.append("symbols", symbol));
  const response = await request(`/v1/signals/stream?${query}`, token, {
    headers: { Accept: "text/event-stream", ...(cursor ? { "Last-Event-ID": cursor } : {}) },
    signal,
  });
  if (!response.ok) await responseJson(response);
  if (!response.body) throw new Error("实时订阅没有返回事件流。");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  try {
    while (!signal.aborted) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value, { stream: !done });
      // Split complete blank-line-delimited SSE records; tolerate CRLF across network chunks.
      let match: RegExpExecArray | null;
      while ((match = /\r\n\r\n|\n\n|\r\r/.exec(pending))) {
        const block = pending.slice(0, match.index);
        pending = pending.slice(match.index + match[0].length);
        const event = parseSignalEvent(block);
        if (event) onEvent(event);
      }
      if (done) break;
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
