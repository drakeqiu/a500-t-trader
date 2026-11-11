const configuredIssuer = process.env.NEXT_PUBLIC_OIDC_ISSUER;
const clientId = process.env.NEXT_PUBLIC_OIDC_CLIENT_ID || "a500-browser-client";
const transactionKey = "a500.oidc.transaction";
const transactionLifetime = 10 * 60 * 1000;
export const configurationReady = Boolean(configuredIssuer);

type Transaction = { state: string; verifier: string; created: number };
export type AccessToken = { token: string; expiresIn: number };

function base64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

// PKCE verifiers generated below are 43 ASCII bytes, so SHA-256 needs one block.
// crypto.subtle is unavailable on non-loopback HTTP origins, but getRandomValues is available.
export function pkceChallenge(verifier: string): string {
  if (!/^[A-Za-z0-9_-]{43}$/.test(verifier)) throw new Error("无效的 PKCE 验证码。 ");
  const constants = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];
  const block = new Uint8Array(64);
  for (let i = 0; i < verifier.length; i++) block[i] = verifier.charCodeAt(i);
  block[verifier.length] = 0x80;
  new DataView(block.buffer).setUint32(60, verifier.length * 8);
  const words = new Uint32Array(64);
  const view = new DataView(block.buffer);
  for (let i = 0; i < 16; i++) words[i] = view.getUint32(i * 4);
  const rotate = (x: number, n: number) => (x >>> n) | (x << (32 - n));
  for (let i = 16; i < 64; i++) {
    const a = words[i - 15],
      b = words[i - 2];
    words[i] =
      (words[i - 16] +
        (rotate(a, 7) ^ rotate(a, 18) ^ (a >>> 3)) +
        words[i - 7] +
        (rotate(b, 17) ^ rotate(b, 19) ^ (b >>> 10))) >>>
      0;
  }
  const initial = [
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ];
  let [a, b, c, d, e, f, g, h] = initial;
  for (let i = 0; i < 64; i++) {
    const t1 =
      (h +
        (rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25)) +
        ((e & f) ^ (~e & g)) +
        constants[i] +
        words[i]) >>>
      0;
    const t2 =
      ((rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
    h = g;
    g = f;
    f = e;
    e = (d + t1) >>> 0;
    d = c;
    c = b;
    b = a;
    a = (t1 + t2) >>> 0;
  }
  const result = new Uint8Array(32);
  const output = new DataView(result.buffer);
  [a, b, c, d, e, f, g, h].forEach((value, i) =>
    output.setUint32(i * 4, (value + initial[i]) >>> 0),
  );
  return base64url(result);
}

async function discover(signal?: AbortSignal) {
  if (!configuredIssuer) throw new Error("缺少 NEXT_PUBLIC_OIDC_ISSUER 配置。");
  const issuer = new URL(configuredIssuer, window.location.origin).toString();
  const discoveryUrl = `${issuer.replace(/\/$/, "")}/.well-known/openid-configuration`;
  const response = await fetch(discoveryUrl, {
    cache: "no-store",
    signal,
  });
  if (!response.ok) throw new Error("无法获取身份服务配置。");
  const config = await response.json();
  if (config.issuer !== issuer) throw new Error("身份服务配置不匹配。");
  const origin = new URL(issuer).origin;
  const endpoint = (value: unknown): string => {
    if (typeof value !== "string") throw new Error("身份服务配置无效。");
    const url = new URL(value);
    if (
      url.origin !== origin ||
      url.username ||
      url.password ||
      !["https:", "http:"].includes(url.protocol)
    )
      throw new Error("身份服务端点无效。");
    return url.href;
  };
  return {
    authorization: endpoint(config.authorization_endpoint),
    token: endpoint(config.token_endpoint),
  };
}

function randomString() {
  if (!globalThis.crypto?.getRandomValues) throw new Error("浏览器不支持安全随机数，无法登录。");
  return base64url(crypto.getRandomValues(new Uint8Array(32)));
}

export async function beginAuthorization(signal?: AbortSignal): Promise<string> {
  const endpoints = await discover(signal);
  const transaction: Transaction = {
    state: randomString(),
    verifier: randomString(),
    created: Date.now(),
  };
  const url = new URL(endpoints.authorization);
  url.search = new URLSearchParams({
    client_id: clientId,
    redirect_uri: `${window.location.origin}/`,
    response_type: "code",
    scope: "openid",
    state: transaction.state,
    code_challenge: pkceChallenge(transaction.verifier),
    code_challenge_method: "S256",
  }).toString();
  sessionStorage.setItem(transactionKey, JSON.stringify(transaction));
  return url.href;
}

// Consume the transaction and erase the callback before any asynchronous work, including failures.
export function completeAuthorization(): Promise<AccessToken> | null {
  const url = new URL(window.location.href);
  if (!["code", "state", "error"].some((key) => url.searchParams.has(key))) return null;
  const params = new URLSearchParams(url.search);
  const raw = sessionStorage.getItem(transactionKey);
  sessionStorage.removeItem(transactionKey);
  for (const key of [
    "code",
    "state",
    "error",
    "error_description",
    "error_uri",
    "session_state",
    "iss",
  ])
    url.searchParams.delete(key);
  window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  return (async () => {
    let transaction: Transaction;
    try {
      transaction = JSON.parse(raw || "null");
    } catch {
      throw new Error("授权会话无效，请重新连接。");
    }
    if (
      !transaction ||
      typeof transaction.state !== "string" ||
      typeof transaction.verifier !== "string" ||
      typeof transaction.created !== "number" ||
      Date.now() - transaction.created > transactionLifetime ||
      Date.now() < transaction.created ||
      params.getAll("state").length !== 1 ||
      params.get("state") !== transaction.state
    )
      throw new Error("授权会话无效或已过期，请重新连接。");
    if (params.has("error"))
      throw new Error(
        params.get("error") === "access_denied" ? "授权已拒绝。" : "身份授权失败，请重新连接。",
      );
    if (params.getAll("code").length !== 1 || !params.get("code"))
      throw new Error("授权回调无效，请重新连接。");
    const endpoints = await discover();
    const response = await fetch(endpoints.token, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        client_id: clientId,
        code: params.get("code")!,
        redirect_uri: `${window.location.origin}/`,
        code_verifier: transaction.verifier,
      }),
    });
    const result = await response.json().catch(() => null);
    if (
      !response.ok ||
      typeof result?.access_token !== "string" ||
      !result.access_token ||
      typeof result.expires_in !== "number" ||
      !Number.isFinite(result.expires_in) ||
      result.expires_in <= 0
    )
      throw new Error("无法完成身份授权，授权码可能已过期，请重新连接。");
    return { token: result.access_token, expiresIn: result.expires_in };
  })();
}
