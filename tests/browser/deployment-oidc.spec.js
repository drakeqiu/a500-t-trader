import { expect, test } from "@playwright/test";
import { configureIdentityProvider, signJwt } from "./oidc-mock.js";
test.use({ ignoreHTTPSErrors: true });

const ISSUER = process.env.A500_E2E_ISSUER;
const BASE_URL = process.env.A500_E2E_GATEWAY_URL;
const JWKS_REGISTER_URL = process.env.A500_E2E_JWKS_REGISTER_URL;
const AUDIENCE = "a500-api";
const CLIENT_ID = "a500-web-client";

test("deployed browser login calls protected account and shared signal APIs", async ({ page }) => {
  const identityRequests = [];
  const failedRequests = [];
  page.on("request", (request) => {
    if (request.url().startsWith(ISSUER)) identityRequests.push(request.url());
  });
  page.on("requestfailed", (request) => failedRequests.push(`${request.url()}: ${request.failure()?.errorText}`));
  let aliceAccessToken = "";
  let bobAccessToken = "";
  await configureIdentityProvider(page, {
    issuer: ISSUER,
    clientId: CLIENT_ID,
    audience: AUDIENCE,
    subject: "deployment-user-alice",
    accessToken: ({ jwk, privateKey, now: tokenTime }) => {
      aliceAccessToken = signJwt({ iss: ISSUER, sub: "deployment-user-alice", aud: AUDIENCE, iat: tokenTime, exp: tokenTime + 300 }, privateKey, jwk);
      bobAccessToken = signJwt({ iss: ISSUER, sub: "deployment-user-bob", aud: AUDIENCE, iat: tokenTime, exp: tokenTime + 300 }, privateKey, jwk);
      return aliceAccessToken;
    },
    onJwks: async (jwk) => {
      const registered = await fetch(JWKS_REGISTER_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(jwk),
      });
      expect(registered.status).toBe(204);
    },
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
