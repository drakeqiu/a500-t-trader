const OIDC_FLOW_KEY = "a500.oidc.pending";
let accessToken = null;
let authConfig = null;
let oidcMetadata = null;

const loginButton = document.querySelector("#login");
const logoutButton = document.querySelector("#logout");
const loadSignalsButton = document.querySelector("#load-signals");
const sessionStatus = document.querySelector("#session-status");
const feedback = document.querySelector("#feedback");
const signals = document.querySelector("#signals");

function randomUrlSafe(bytes = 32) {
  const value = crypto.getRandomValues(new Uint8Array(bytes));
  return toUrlSafe(value);
}

function toUrlSafe(bytes) {
  let binary = "";
  for (const value of bytes) binary += String.fromCharCode(value);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

async function sha256UrlSafe(value) {
  return toUrlSafe(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value))));
}

async function loadConfig() {
  if (authConfig) return authConfig;
  const response = await fetch("/web-config", { credentials: "omit", cache: "no-store" });
  if (!response.ok) throw new Error("登录服务尚未配置，请联系运维人员。");
  authConfig = await response.json();
  return authConfig;
}

async function loadMetadata() {
  if (oidcMetadata) return oidcMetadata;
  const { issuer } = await loadConfig();
  const response = await fetch(`${issuer}/.well-known/openid-configuration`, {
    credentials: "omit",
    cache: "no-store",
  });
  if (!response.ok) throw new Error("无法读取身份提供方配置。");
  const metadata = await response.json();
  if (metadata.issuer !== issuer) throw new Error("身份提供方签发者与配置不一致。");
  const issuerUrl = new URL(issuer);
  if (issuerUrl.protocol !== "https:") throw new Error("身份提供方必须使用 HTTPS。");
  for (const endpoint of [metadata.authorization_endpoint, metadata.token_endpoint, metadata.jwks_uri]) {
    if (!endpoint || new URL(endpoint).protocol !== "https:") {
      throw new Error("身份提供方返回了无效端点。");
    }
  }
  oidcMetadata = metadata;
  return metadata;
}

async function beginLogin() {
  const [config, metadata] = await Promise.all([loadConfig(), loadMetadata()]);
  const state = randomUrlSafe();
  const nonce = randomUrlSafe();
  const verifier = randomUrlSafe(48);
  const challenge = await sha256UrlSafe(verifier);
  sessionStorage.setItem(OIDC_FLOW_KEY, JSON.stringify({ state, nonce, verifier }));

  const authorization = new URL(metadata.authorization_endpoint);
  authorization.search = new URLSearchParams({
    client_id: config.clientId,
    redirect_uri: config.redirectUri,
    response_type: "code",
    scope: "openid profile",
    state,
    nonce,
    code_challenge: challenge,
    code_challenge_method: "S256",
    resource: config.audience,
  }).toString();
  window.location.assign(authorization);
}

function decodeBase64Url(value) {
  const padded = value.replaceAll("-", "+").replaceAll("_", "/") + "===".slice((value.length + 3) % 4);
  return Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
}

function decodeJwtPart(value) {
  return JSON.parse(new TextDecoder().decode(decodeBase64Url(value)));
}

async function verifyIdToken(token, expectedNonce, config, metadata, tokenResponse) {
  const parts = token.split(".");
  if (parts.length !== 3) throw new Error("身份提供方返回了无效 ID 令牌。");
  const header = decodeJwtPart(parts[0]);
  const claims = decodeJwtPart(parts[1]);
  if (!header.kid || !["RS256", "ES256"].includes(header.alg)) throw new Error("ID 令牌签名算法不受支持。");

  const keysResponse = await fetch(metadata.jwks_uri, { credentials: "omit", cache: "no-store" });
  if (!keysResponse.ok) throw new Error("无法验证身份提供方签名。");
  const jwks = await keysResponse.json();
  const jwk = jwks.keys?.find((key) => key.kid === header.kid
    && (!key.alg || key.alg === header.alg)
    && (!key.use || key.use === "sig"));
  if (!jwk) throw new Error("找不到用于验证 ID 令牌的密钥。");
  const algorithm = header.alg === "RS256"
    ? { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }
    : { name: "ECDSA", namedCurve: "P-256" };
  const key = await crypto.subtle.importKey("jwk", jwk, algorithm, false, ["verify"]);
  const verifyAlgorithm = header.alg === "RS256" ? "RSASSA-PKCS1-v1_5" : { name: "ECDSA", hash: "SHA-256" };
  const validSignature = await crypto.subtle.verify(
    verifyAlgorithm,
    key,
    decodeBase64Url(parts[2]),
    new TextEncoder().encode(`${parts[0]}.${parts[1]}`),
  );
  const audience = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  const now = Math.floor(Date.now() / 1000);
  const authorizedPartyValid = audience.length === 1
    ? claims.azp === undefined || claims.azp === config.clientId
    : claims.azp === config.clientId;
  if (!validSignature || claims.iss !== config.issuer || !audience.includes(config.clientId)
      || !authorizedPartyValid || claims.nonce !== expectedNonce || !Number.isFinite(claims.exp) || claims.exp <= now
      || (claims.nbf !== undefined && claims.nbf > now)) {
    throw new Error("身份验证结果无效或已过期。");
  }
  if (claims.at_hash) {
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(tokenResponse.access_token)));
    if (claims.at_hash !== toUrlSafe(digest.slice(0, digest.length / 2))) {
      throw new Error("身份令牌与 API 访问令牌不匹配。");
    }
  }
  return;
}

async function finishLogin() {
  const query = new URLSearchParams(window.location.search);
  if (!query.has("code") && !query.has("error")) return;
  const stored = sessionStorage.getItem(OIDC_FLOW_KEY);
  sessionStorage.removeItem(OIDC_FLOW_KEY);
  window.history.replaceState(null, "", window.location.pathname);
  if (query.has("error")) throw new Error("身份提供方未授权此次登录。");
  if (!stored) throw new Error("登录状态已失效，请重新登录。");

  const pending = JSON.parse(stored);
  if (!query.get("state") || query.get("state") !== pending.state) throw new Error("登录状态校验失败。");
  const [config, metadata] = await Promise.all([loadConfig(), loadMetadata()]);
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: config.clientId,
    code: query.get("code"),
    redirect_uri: config.redirectUri,
    code_verifier: pending.verifier,
    resource: config.audience,
  });
  const response = await fetch(metadata.token_endpoint, {
    method: "POST",
    credentials: "omit",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  const tokenResponse = await response.json();
  if (!response.ok || typeof tokenResponse.access_token !== "string" || !tokenResponse.access_token
      || typeof tokenResponse.token_type !== "string" || tokenResponse.token_type.toLowerCase() !== "bearer") {
    throw new Error("身份提供方拒绝了登录请求。");
  }
  if (typeof tokenResponse.id_token !== "string") throw new Error("身份提供方未返回 ID 令牌。");
  await verifyIdToken(tokenResponse.id_token, pending.nonce, config, metadata, tokenResponse);
  accessToken = tokenResponse.access_token;
  updateSession(true);
  feedback.textContent = "登录成功。选择“读取信号”访问受保护 API。";
  feedback.classList.remove("error");
}

function updateSession(isAuthenticated) {
  sessionStatus.textContent = isAuthenticated ? "已登录" : "尚未登录";
  loginButton.hidden = isAuthenticated;
  logoutButton.hidden = !isAuthenticated;
  loadSignalsButton.disabled = !isAuthenticated;
  if (!isAuthenticated) signals.replaceChildren();
}

function clearSession(message) {
  accessToken = null;
  sessionStorage.removeItem(OIDC_FLOW_KEY);
  updateSession(false);
  feedback.textContent = message;
  feedback.classList.remove("error");
}

function displayError(message) {
  feedback.textContent = message;
  feedback.classList.add("error");
}

async function readSignals() {
  if (!accessToken) return;
  const response = await fetch("/v1/signals", {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
    credentials: "omit",
    cache: "no-store",
  });
  if (response.status === 401 || response.status === 403) {
    clearSession("登录已失效，请重新登录后继续。");
    return;
  }
  if (!response.ok) throw new Error(`读取信号失败（HTTP ${response.status}）。`);
  const result = await response.json();
  signals.replaceChildren(...result.signals.map(renderSignal));
  feedback.textContent = `${result.signals.length} 个标的已更新。`;
  feedback.classList.remove("error");
}

function renderSignal(signal) {
  const row = document.createElement("article");
  row.className = "signal-row";
  const symbol = document.createElement("span");
  symbol.className = "signal-symbol";
  symbol.textContent = signal.symbol;
  const meta = document.createElement("span");
  meta.className = "signal-meta";
  meta.textContent = signal.bar_timestamp || "暂无新鲜数据";
  const direction = document.createElement("span");
  direction.className = `signal-direction${signal.status === "UNAVAILABLE" ? " unavailable" : ""}`;
  direction.textContent = signal.direction || signal.status || "UNAVAILABLE";
  row.append(symbol, meta, direction);
  return row;
}

loginButton.addEventListener("click", () => beginLogin().catch((error) => displayError(error.message)));
logoutButton.addEventListener("click", () => clearSession("已退出登录。"));
loadSignalsButton.addEventListener("click", () => readSignals().catch((error) => displayError(error.message)));

finishLogin().catch((error) => {
  clearSession(error.message);
  displayError(error.message);
});
