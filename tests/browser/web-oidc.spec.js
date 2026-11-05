import { expect, test } from "@playwright/test";
import { configureIdentityProvider } from "./oidc-mock.js";
const ISSUER = "https://identity.example.test";
const ACCESS_TOKEN = "browser-test-access-token";
test("PKCE callback keeps tokens in memory and sends Bearer to the protected API", async ({ page }) => {
  await configureIdentityProvider(page, {
    issuer: ISSUER,
    clientId: "a500-web-client",
    audience: "a500-api",
    subject: "approved-user",
    accessToken: ACCESS_TOKEN,
    onAuthorize: (authorization) => {
      expect(authorization.get("scope")).toContain("openid");
    },
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
  await configureIdentityProvider(page, {
    issuer: ISSUER,
    clientId: "a500-web-client",
    audience: "a500-api",
    subject: "approved-user",
    accessToken: ACCESS_TOKEN,
    deny: true,
    onToken: () => {
      throw new Error("token exchange must not happen after provider denial");
    },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "使用工作账户登录" }).click();
  await expect(page.getByText("尚未登录")).toBeVisible();
  await expect(page.getByText("身份提供方未授权此次登录。", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "读取信号" })).toBeDisabled();
  expect(await page.evaluate(() => ({ localStorage: Object.keys(localStorage), sessionStorage: Object.keys(sessionStorage) })))
    .toEqual({ localStorage: [], sessionStorage: [] });
});

test("a rejected protected request clears the in-memory session", async ({ page }) => {
  await configureIdentityProvider(page, {
    issuer: ISSUER,
    clientId: "a500-web-client",
    audience: "a500-api",
    subject: "approved-user",
    accessToken: ACCESS_TOKEN,
  });
  await page.goto("/");
  await page.getByRole("button", { name: "使用工作账户登录" }).click();
  await expect(page.getByText("已登录")).toBeVisible();
  await page.route("**/v1/signals", (route) => route.fulfill({ status: 401, json: { detail: "invalid token" } }));
  await page.getByRole("button", { name: "读取信号" }).click();
  await expect(page.getByText("尚未登录")).toBeVisible();
  await expect(page.getByText("登录已失效，请重新登录后继续。")).toBeVisible();
});
