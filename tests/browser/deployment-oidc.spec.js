import { createHash, generateKeyPairSync, sign } from "node:crypto";
import { expect, test } from "@playwright/test";
test.use({ ignoreHTTPSErrors: true });

const ISSUER = process.env.A500_E2E_ISSUER;
const BASE_URL = process.env.A500_E2E_GATEWAY_URL;
const JWKS_REGISTER_URL = process.env.A500_E2E_JWKS_REGISTER_URL;
const AUDIENCE = "a500-api";
const CLIENT_ID = "a500-web-client";
const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
};

function base64url(value) {
  return Buffer.from(value).toString("base64url");
}

function signJwt(claims, privateKey, jwk) {
  const header = base64url(JSON.stringify({ alg: "RS256", kid: jwk.kid, typ: "JWT" }));
  const payload = base64url(JSON.stringify(claims));
  const input = `${header}.${payload}`;
  return `${input}.${sign("RSA-SHA256", Buffer.from(input), privateKey).toString("base64url")}`;
}

test("deployed browser login calls protected account and shared signal APIs", async ({ page }) => {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwk = { ...publicKey.export({ format: "jwk" }), kid: "compose-e2e-key", alg: "RS256", use: "sig" };
  const now = Math.floor(Date.now() / 1000);
  const aliceAccessToken = signJwt({ iss: ISSUER, sub: "deployment-user-alice", aud: AUDIENCE, iat: now, exp: now + 300 }, privateKey, jwk);
  const bobAccessToken = signJwt({ iss: ISSUER, sub: "deployment-user-bob", aud: AUDIENCE, iat: now, exp: now + 300 }, privateKey, jwk);
  let nonce = "";
  let challenge = "";

  const registered = await fetch(JWKS_REGISTER_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(jwk),
  });
  expect(registered.status).toBe(204);

  const identityRequests = [];
  const failedRequests = [];
  page.on("request", (request) => {
    if (request.url().startsWith(ISSUER)) identityRequests.push(request.url());
  });
  page.on("requestfailed", (request) => failedRequests.push(`${request.url()}: ${request.failure()?.errorText}`));
  await page.route(`${ISSUER}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (url.pathname === "/.well-known/openid-configuration") {
      return route.fulfill({ json: {
        issuer: ISSUER,
        authorization_endpoint: `${ISSUER}/authorize`,
        token_endpoint: `${ISSUER}/token`,
        jwks_uri: `${ISSUER}/jwks`,
      }, headers: cors });
    }
    if (url.pathname === "/jwks") return route.fulfill({ json: { keys: [jwk] }, headers: cors });
    if (url.pathname === "/authorize") {
      const authorization = url.searchParams;
      expect(authorization.get("client_id")).toBe(CLIENT_ID);
      expect(authorization.get("response_type")).toBe("code");
      expect(authorization.get("code_challenge_method")).toBe("S256");
      expect(authorization.get("resource")).toBe(AUDIENCE);
      nonce = authorization.get("nonce");
      challenge = authorization.get("code_challenge");
      const callback = new URL(authorization.get("redirect_uri"));
      callback.searchParams.set("code", "one-time-code");
      callback.searchParams.set("state", authorization.get("state"));
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: `<script>location.replace(${JSON.stringify(callback.toString())})</script>`,
      });
    }
    if (url.pathname === "/token" && request.method() === "POST") {
      const form = new URLSearchParams(request.postData() ?? "");
      expect(form.get("grant_type")).toBe("authorization_code");
      expect(form.get("client_id")).toBe(CLIENT_ID);
      expect(form.get("resource")).toBe(AUDIENCE);
      const verifier = form.get("code_verifier");
      expect(createHash("sha256").update(verifier).digest("base64url")).toBe(challenge);
      const idToken = signJwt({ iss: ISSUER, aud: CLIENT_ID, sub: "deployment-user-alice", nonce, iat: now, exp: now + 300 }, privateKey, jwk);
      return route.fulfill({ json: { access_token: aliceAccessToken, token_type: "Bearer", expires_in: 300, id_token: idToken }, headers: cors });
    }
    return route.fulfill({ status: 404, headers: cors });
  });

  await page.goto(BASE_URL);
  await page.getByRole("button", { name: "使用工作账户登录" }).click();
  await expect(page.getByText("已登录")).toBeVisible({ timeout: 10000 }).catch(async (error) => {
    throw new Error(
      `${error.message}; url=${page.url()}; identity=${JSON.stringify(identityRequests)}; failed=${JSON.stringify(failedRequests)}; body=${await page.locator("body").textContent()}`,
    );
  });
  expect(await page.evaluate(() => ({ localStorage: Object.keys(localStorage), sessionStorage: Object.keys(sessionStorage), cookies: document.cookie })))
    .toEqual({ localStorage: [], sessionStorage: [], cookies: "" });

  const signalResponse = page.waitForResponse((response) => new URL(response.url()).pathname === "/v1/signals");
  await page.getByRole("button", { name: "读取信号" }).click();
  expect((await signalResponse).status()).toBe(200);

  const unauthenticated = await page.evaluate(async () => (await fetch("/v1/signals")).status);
  expect(unauthenticated).toBe(401);
  const account = await page.evaluate(async (token) => {
    const snapshot = {
      as_of: new Date().toISOString(),
      holdings: {},
      protected_floor: {},
      sellable: {},
      cost_basis: {},
      available_cash: "123.45",
    };
    const created = await fetch("/v1/account-snapshot", {
      method: "PUT",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", "If-None-Match": "*" },
      body: JSON.stringify(snapshot),
    });
    const read = await fetch("/v1/account-snapshot", { headers: { Authorization: `Bearer ${token}` } });
    return { created: created.status, read: read.status, snapshot: await read.json() };
  }, aliceAccessToken);
  expect(account.created).toBe(201);
  expect(account.read).toBe(200);
  expect(account.snapshot.available_cash).toBe("123.45");

  const isolated = await page.evaluate(async (token) => {
    const response = await fetch("/v1/account-snapshot", { headers: { Authorization: `Bearer ${token}` } });
    return response.status;
  }, bobAccessToken);
  expect(isolated).toBe(404);

  const sharedSignals = await page.evaluate(async (tokens) => {
    const results = await Promise.all(tokens.map(async (token) => {
      const response = await fetch("/v1/signals", { headers: { Authorization: `Bearer ${token}` } });
      return { status: response.status, body: await response.text() };
    }));
    return results;
  }, [aliceAccessToken, bobAccessToken]);
  expect(sharedSignals[0].status).toBe(200);
  expect(sharedSignals[1]).toEqual(sharedSignals[0]);

  const openapi = await page.evaluate(async (token) => {
    const denied = await fetch("/openapi.json");
    const allowed = await fetch("/openapi.json", { headers: { Authorization: `Bearer ${token}` } });
    return { denied: denied.status, allowed: allowed.status };
  }, aliceAccessToken);
  expect(openapi).toEqual({ denied: 401, allowed: 200 });
});
