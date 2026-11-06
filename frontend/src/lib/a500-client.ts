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

export type ImportPreview = {
  batch_id: string;
  valid: number;
  new: number;
  duplicates: number;
  errors: number;
  error_details: Array<{ row?: number; field?: string; reason?: string }>;
  confirmed?: unknown;
};

type SignalResponse = { signals: Signal[]; strategy: string; parameter_fingerprint: string };
const apiOrigin = process.env.NEXT_PUBLIC_A500_API_BASE_URL?.replace(/\/$/, "");
const issuer = process.env.NEXT_PUBLIC_A500_OIDC_ISSUER?.replace(/\/$/, "");
const clientId = process.env.NEXT_PUBLIC_A500_OIDC_CLIENT_ID || "a500-device-client";
export const configurationReady = Boolean(apiOrigin && issuer);

async function responseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    if (response.status === 401) throw new Error("授权已失效，请重新连接数据服务。");
    const body = await response.json().catch(() => null);
    throw new Error(body?.detail || `服务请求失败（${response.status}）`);
  }
  return response.json() as Promise<T>;
}

export async function discover(): Promise<{ device: string; token: string }> {
  if (!issuer) throw new Error("缺少 OIDC issuer 配置。");
  const response = await fetch(`${issuer}/.well-known/openid-configuration`, { cache: "no-store" });
  const config = await responseJson<{
    device_authorization_endpoint: string;
    token_endpoint: string;
  }>(response);
  return { device: config.device_authorization_endpoint, token: config.token_endpoint };
}

export async function requestDeviceCode(endpoint: string) {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: clientId, scope: "openid" }),
  });
  return responseJson<{
    device_code: string;
    user_code: string;
    verification_uri: string;
    verification_uri_complete?: string;
    interval?: number;
    expires_in: number;
  }>(response);
}

export async function pollDeviceToken(
  endpoint: string,
  deviceCode: string,
  interval: number,
  signal: AbortSignal,
): Promise<string> {
  let intervalMs = Math.max(1, interval) * 1000;
  while (!signal.aborted) {
    const { promise, resolve, reject } = Promise.withResolvers<void>();
    const timer = window.setTimeout(resolve, intervalMs);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
    await promise;
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "urn:ietf:params:oauth:grant-type:device_code",
        device_code: deviceCode,
        client_id: clientId,
      }),
      signal,
    });
    const result = await response.json();
    if (response.ok && result.access_token) return result.access_token as string;
    if (result.error === "authorization_pending") continue;
    if (result.error === "slow_down") {
      intervalMs += 5_000;
      continue;
    }
    if (result.error === "access_denied") throw new Error("授权已拒绝。");
    if (result.error === "expired_token") throw new Error("授权码已过期，请重新开始。");
    throw new Error(result.error_description || "无法完成身份授权。");
  }
  throw new DOMException("Aborted", "AbortError");
}

async function api<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  if (!apiOrigin) throw new Error("缺少 API 地址配置。");
  const response = await fetch(`${apiOrigin}${path}`, {
    ...init,
    cache: "no-store",
    headers: { Authorization: `Bearer ${token}`, ...init?.headers },
  });
  return responseJson<T>(response);
}

export async function getSignals(token: string): Promise<Signal[]> {
  return (await api<SignalResponse>("/v1/signals", token)).signals;
}
export function getNav(token: string) {
  return api<AccountNav>("/v1/account-nav", token);
}
export function uploadPreview(token: string, file: File) {
  const body = new FormData();
  body.append("file", file);
  return api<ImportPreview>("/v1/trade-imports/previews", token, { method: "POST", body });
}
export function confirmPreview(token: string, batchId: string) {
  return api<ImportPreview>(
    `/v1/trade-imports/previews/${encodeURIComponent(batchId)}/confirm`,
    token,
    { method: "POST" },
  );
}

export async function streamSignals(
  token: string,
  symbols: string[],
  signal: AbortSignal,
  onSignal: (signal: Signal) => void,
) {
  if (!apiOrigin || symbols.length === 0) return;
  const query = new URLSearchParams();
  symbols.forEach((symbol) => query.append("symbols", symbol));
  const response = await fetch(`${apiOrigin}/v1/signals/stream?${query}`, {
    headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
    cache: "no-store",
    signal,
  });
  if (!response.ok || !response.body)
    throw new Error(
      response.status === 401
        ? "授权已失效，请重新连接数据服务。"
        : `实时订阅失败（${response.status}）`,
    );
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  while (!signal.aborted) {
    const { value, done } = await reader.read();
    if (done) break;
    pending += decoder.decode(value, { stream: true });
    const blocks = pending.split(/\r?\n\r?\n/);
    pending = blocks.pop() || "";
    for (const block of blocks) {
      const data = block
        .split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim())
        .join("\n");
      if (!data) continue;
      try {
        const event: unknown = JSON.parse(data);
        if (isSignal(event)) onSignal(event);
      } catch {
        /* Ignore malformed frames; the next snapshot or update remains authoritative. */
      }
    }
  }
}

function isSignal(value: unknown): value is Signal {
  return (
    typeof value === "object" &&
    value !== null &&
    "symbol" in value &&
    typeof value.symbol === "string" &&
    "status" in value &&
    typeof value.status === "string"
  );
}
