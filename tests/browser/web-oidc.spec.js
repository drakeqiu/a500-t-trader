import { createHash, generateKeyPairSync, sign } from "node:crypto";
import { expect, test } from "@playwright/test";

const ISSUER = "https://identity.example.test";
const ACCESS_TOKEN = "browser-test-access-token";
const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
};

function base64url(value) {
  return Buffer.from(value).toString("base64url");
}

function createIdToken(nonce, privateKey, jwk) {
  const header = base64url(JSON.stringify({ alg: "RS256", kid: jwk.kid, typ: "JWT" }));
  const claims = base64url(JSON.stringify({
    iss: ISSUER,
    aud: "a500-web-client",
    sub: "approved-user",
    nonce,
    iat: Math.floor(Date.now() / 1000),
    exp: Math.floor(Date.now() / 1000) + 300,
  }));
  const input = `${header}.${claims}`;
  return `${input}.${sign("RSA-SHA256", Buffer.from(input), privateKey).toString("base64url")}`;
}

async function configureIdentityProvider(page, verifyGrant, deny = false) {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwk = { ...publicKey.export({ format: "jwk" }), kid: "test-signing-key", alg: "RS256", use: "sig" };
  let nonce = "";
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
      expect(authorization.get("client_id")).toBe("a500-web-client");
      expect(authorization.get("response_type")).toBe("code");
      expect(authorization.get("scope")).toContain("openid");
      expect(authorization.get("code_challenge_method")).toBe("S256");
      expect(authorization.get("resource")).toBe("a500-api");
      nonce = authorization.get("nonce");
      const callback = new URL(authorization.get("redirect_uri"));
      if (deny) callback.searchParams.set("error", "access_denied");
      else callback.searchParams.set("code", "one-time-code");
      callback.searchParams.set("state", authorization.get("state"));
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: `<script>location.replace(${JSON.stringify(callback.toString())})</script>`,
      });
    }
    if (url.pathname === "/token" && request.method() === "POST") {
      const form = new URLSearchParams(request.postData() ?? "");
      verifyGrant(form);
      return route.fulfill({ json: {
        access_token: ACCESS_TOKEN,
        token_type: "Bearer",
        expires_in: 300,
        id_token: createIdToken(nonce, privateKey, jwk),
      }, headers: cors });
    }
    return route.fulfill({ status: 404, headers: cors });
  });
}

test("PKCE callback keeps tokens in memory and sends Bearer to the protected API", async ({ page }) => {
  let challenge = "";
  await configureIdentityProvider(page, (form) => {
    expect(form.get("grant_type")).toBe("authorization_code");
    expect(form.get("client_id")).toBe("a500-web-client");
    expect(form.get("resource")).toBe("a500-api");
    const verifier = form.get("code_verifier");
    expect(verifier).toBeTruthy();
    expect(createHash("sha256").update(verifier).digest("base64url")).toBe(challenge);
  });
  await page.route(`${ISSUER}/authorize**`, (route) => {
    challenge = new URL(route.request().url()).searchParams.get("code_challenge");
    return route.fallback();
  });
  await page.goto("/");
  await expect(page.getByRole("link", { name: /注册|创建账户/ })).toHaveCount(0);
  await page.getByRole("button", { name: "使用工作账户登录" }).click();
  await expect(page.getByText("已登录")).toBeVisible();
  expect(await page.evaluate(() => ({
    localStorage: Object.keys(localStorage),
    sessionStorage: Object.keys(sessionStorage),
    cookies: document.cookie,
  }))).toEqual({ localStorage: [], sessionStorage: [], cookies: "" });

  const apiRequest = page.waitForRequest((request) => new URL(request.url()).pathname === "/v1/signals");
  await page.getByRole("button", { name: "读取信号" }).click();
  expect((await apiRequest).headers().authorization).toBe(`Bearer ${ACCESS_TOKEN}`);
  await expect(page.getByText("510300", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page.getByText("尚未登录")).toBeVisible();
  await expect(page.getByRole("button", { name: "读取信号" })).toBeDisabled();
  expect(await page.evaluate(() => ({ localStorage: Object.keys(localStorage), sessionStorage: Object.keys(sessionStorage) })))
    .toEqual({ localStorage: [], sessionStorage: [] });
});

test("an identity-provider denial never creates an application session or registration flow", async ({ page }) => {
  await configureIdentityProvider(page, () => {
    throw new Error("token exchange must not happen after provider denial");
  }, true);
  await page.goto("/");
  await page.getByRole("button", { name: "使用工作账户登录" }).click();
  await expect(page.getByText("尚未登录")).toBeVisible();
  await expect(page.getByText("身份提供方未授权此次登录。", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "读取信号" })).toBeDisabled();
  expect(await page.evaluate(() => ({ localStorage: Object.keys(localStorage), sessionStorage: Object.keys(sessionStorage) })))
    .toEqual({ localStorage: [], sessionStorage: [] });
});

test("a rejected protected request clears the in-memory session", async ({ page }) => {
  await configureIdentityProvider(page, () => {});
  await page.goto("/");
  await page.getByRole("button", { name: "使用工作账户登录" }).click();
  await expect(page.getByText("已登录")).toBeVisible();
  await page.route("**/v1/signals", (route) => route.fulfill({ status: 401, json: { detail: "invalid token" } }));
  await page.getByRole("button", { name: "读取信号" }).click();
  await expect(page.getByText("尚未登录")).toBeVisible();
  await expect(page.getByText("登录已失效，请重新登录后继续。")).toBeVisible();
});
