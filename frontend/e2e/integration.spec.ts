import { randomUUID } from "node:crypto";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

// Real Python account/signal routes, ephemeral SQLite and fixture Redis.
// Only identity authorization and market-state inputs are deterministic fixtures;
// this suite is not evidence of a live Keycloak or market-provider connection.
const fixture = `http://127.0.0.1:${process.env.A500_E2E_FIXTURE_PORT || "38100"}`;

async function connect(page: Page, subject: string) {
  const issuedToken = page.waitForResponse(
    (response) => response.url() === `${fixture}/fixture-idp/token` && response.ok(),
  );
  await page.getByRole("button", { name: "安全连接", exact: true }).click();
  const verification = page.context().waitForEvent("page");
  await page.getByRole("link", { name: /打开验证页面/ }).click();
  const popup = await verification;
  await popup.getByLabel("Fixture account").fill(subject);
  await popup.getByRole("button", { name: "Authorize fixture device" }).click();
  await expect(popup.getByRole("heading", { name: "Fixture device authorized" })).toBeVisible();
  await popup.close();
  const token = (await (await issuedToken).json()).access_token as string;
  await expect(page.getByRole("heading", { name: "实时信号", exact: true })).toBeVisible();
  return token;
}

async function fillSnapshot(page: Page, cash = "125.50") {
  await page.getByLabel("快照时间（含时区）").fill(new Date().toISOString());
  await page.getByLabel("可用现金", { exact: true }).fill(cash);
  await page.getByLabel("持仓数量（JSON）").fill('{"510300":100}');
  await page.getByLabel("保护底仓（JSON）").fill('{"510300":20}');
  await page.getByLabel("可卖数量（JSON）").fill('{"510300":80}');
  await page.getByLabel("成本基础（JSON）").fill('{"510300":"3.8500"}');
}

async function saveSnapshot(page: Page, status: number) {
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/account-snapshot") && response.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "保存账户快照", exact: true }).click();
  const response = await saved;
  expect(response.status()).toBe(status);
  return response;
}

async function upload(page: Page, request: APIRequestContext, kind: "valid" | "invalid") {
  const csv = await request.get(`${fixture}/__fixture/csv/${kind}`);
  expect(csv.ok()).toBeTruthy();
  await page.getByLabel("选择成交文件").setInputFiles({
    name: `${kind}.csv`,
    mimeType: "text/csv",
    buffer: await csv.body(),
  });
  const preview = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/trade-imports/previews") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "预览文件", exact: true }).click();
  const response = await preview;
  expect(response.status()).toBe(200);
  return response.json() as Promise<{ batch_id: string; errors: number; new: number }>;
}

test("serves the UI and API from one origin without the legacy API route", async ({
  page,
  request,
}) => {
  await page.goto("/");
  expect(new URL(page.url()).origin).toBe(new URL(test.info().project.use.baseURL!).origin);
  expect((await request.get("/api/v1/account-snapshot")).status()).toBe(401);
  expect((await request.get("/v1/account-snapshot")).status()).toBe(404);
});

test("creates and edits an account; a real 412 retains the draft until explicitly replaced", async ({
  page,
  context,
  request,
}) => {
  const unauthorized = await request.get("/api/v1/account-snapshot");
  expect(unauthorized.status()).toBe(401);
  await page.goto("/");
  const subject = `snapshot-${randomUUID()}`;
  const token = await connect(page, subject);
  const nav = page.getByRole("region", { name: "账户净资产" });
  await expect(nav).toContainText(/不可用|不可估值|暂无|尚无/);
  await fillSnapshot(page);
  const created = await saveSnapshot(page, 201);
  expect(created.headers().etag).toBeTruthy();
  expect(created.request().headers()["if-none-match"]).toBe("*");
  await expect(nav).toContainText("market_data_unavailable");
  await expect(nav.locator(".nav-value")).toHaveCount(0);

  const other = await context.newPage();
  await other.goto("/");
  await connect(other, subject);
  await expect(other.getByLabel("可用现金", { exact: true })).toHaveValue("125.50");
  await page.getByLabel("可用现金", { exact: true }).fill("999.00");
  await other.getByLabel("可用现金", { exact: true }).fill("456.00");
  const edited = await saveSnapshot(other, 200);
  expect(edited.headers().etag).not.toBe(created.headers().etag);

  const conflict = await saveSnapshot(page, 412);
  expect(conflict.request().headers()["if-match"]).toBe(created.headers().etag);
  await expect(page.getByRole("alert").filter({ hasText: "草稿已保留" })).toBeVisible();
  await expect(page.getByLabel("可用现金", { exact: true })).toHaveValue("999.00");
  const latest = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/account-snapshot") &&
      response.request().method() === "GET" &&
      response.ok(),
  );
  await page.getByRole("button", { name: "读取最新快照", exact: true }).click();
  expect((await (await latest).json()).available_cash).toBe("456.00");
  await expect(
    page.getByRole("button", { name: "使用最新快照替换草稿", exact: true }),
  ).toBeEnabled();
  await expect(page.getByLabel("可用现金", { exact: true })).toHaveValue("999.00");
  await page.getByRole("button", { name: "使用最新快照替换草稿", exact: true }).click();
  await expect(page.getByLabel("可用现金", { exact: true })).toHaveValue("456.00");
  await page.getByLabel("可用现金", { exact: true }).fill("789.00");
  await saveSnapshot(page, 200);
  const current = await request.get("/api/v1/account-snapshot", {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect((await current.json()).available_cash).toBe("789.00");
  await other.close();
});

test("shows sanitized row errors, restores preview after reauthorization, and confirms without changing the snapshot", async ({
  page,
  request,
}) => {
  await page.goto("/");
  const subject = `import-${randomUUID()}`;
  let token = await connect(page, subject);
  await fillSnapshot(page);
  await saveSnapshot(page, 201);
  const before = await request.get("/api/v1/account-snapshot", {
    headers: { Authorization: `Bearer ${token}` },
  });
  const originalSnapshot = await before.json();
  const imports = page.getByRole("region", { name: "成交文件导入" });
  const invalid = await upload(page, request, "invalid");
  expect(invalid.errors).toBe(1);
  await expect(imports).toContainText(/3.*成交数量/);
  await expect(imports).not.toContainText("PRIVATE-ACCOUNT");
  await expect(imports).not.toContainText("PRIVATE-BALANCE");
  await expect(imports.getByRole("button", { name: "确认导入", exact: true })).toHaveCount(0);

  const valid = await upload(page, request, "valid");
  expect(valid.errors).toBe(0);
  expect(valid.new).toBe(1);
  await expect(imports).toContainText(valid.batch_id);
  const stored = await page.evaluate(() =>
    JSON.stringify({
      local: Object.entries(localStorage),
      session: Object.entries(sessionStorage),
    }),
  );
  expect(stored).not.toContain(token);
  await page.reload();
  await expect(page.getByRole("button", { name: "安全连接", exact: true })).toBeEnabled();
  const restored = page.waitForResponse((response) =>
    response.url().endsWith(`/api/v1/trade-imports/previews/${valid.batch_id}`),
  );
  token = await connect(page, subject);
  expect((await restored).status()).toBe(200);
  await expect(imports).toContainText(valid.batch_id);
  await expect(page.getByRole("status").filter({ hasText: "已恢复导入批次" })).toBeVisible();
  const confirmed = page.waitForResponse((response) =>
    response.url().endsWith(`/api/v1/trade-imports/previews/${valid.batch_id}/confirm`),
  );
  await imports.getByRole("button", { name: "确认导入", exact: true }).click();
  const confirmation = await confirmed;
  expect(confirmation.status()).toBe(200);
  expect((await confirmation.json()).new).toBe(1);
  await expect(imports.getByRole("heading", { name: "已确认", exact: true })).toBeVisible();
  const after = await request.get("/api/v1/account-snapshot", {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(after.headers().etag).toBe(before.headers().etag);
  expect(await after.json()).toEqual(originalSnapshot);

  await page.reload();
  await connect(page, subject);
  await expect(imports).toContainText(valid.batch_id);
  await expect(imports.getByRole("heading", { name: "已确认", exact: true })).toBeVisible();
  const duplicate = await upload(page, request, "valid");
  expect(duplicate.new).toBe(0);
  const duplicateCount = imports.locator("dl > div").filter({
    has: page.locator("dt", { hasText: /^重复$/ }),
  });
  await expect(duplicateCount.locator("dd")).toHaveText("1");
});

test("consumes real SSE and resumes its cursor; expired history resets to authoritative current state", async ({
  page,
  request,
}) => {
  await request.post(`${fixture}/__fixture/expire-events`);
  await page.addInitScript(() => {
    const read = ReadableStreamDefaultReader.prototype.read;
    const decoder = new TextDecoder();
    Reflect.set(window, "__sseBytes", "");
    ReadableStreamDefaultReader.prototype.read = async function () {
      const result = await read.call(this);
      if (result.value instanceof Uint8Array) {
        Reflect.set(
          window,
          "__sseBytes",
          Reflect.get(window, "__sseBytes") + decoder.decode(result.value, { stream: true }),
        );
      }
      return result;
    };
  });
  await request.post(`${fixture}/__fixture/signals/WAIT`);
  const cursors: string[] = [];
  let signalReads = 0;
  page.on("response", (response) => {
    if (new URL(response.url()).pathname === "/api/v1/signals") signalReads += 1;
  });
  page.on("request", (outgoing) => {
    if (new URL(outgoing.url()).pathname === "/api/v1/signals/stream") {
      cursors.push(outgoing.headers()["last-event-id"] || "");
    }
  });
  await page.goto("/");
  await connect(page, `stream-${randomUUID()}`);
  const row = page
    .getByRole("row")
    .filter({ has: page.getByRole("rowheader", { name: "563360" }) });
  await expect(row).toContainText("WAIT");
  await expect(page.getByRole("region", { name: "当前策略信号" })).toContainText("实时订阅");
  // The status label precedes the network subscription. Wait for the initial
  // SSE snapshot to trigger its current-state read before publishing an ID.
  await expect.poll(() => signalReads).toBeGreaterThanOrEqual(2);
  const published = await request.post(`${fixture}/__fixture/signals/BUY`);
  const buyId = (await published.json()).event_id as string;
  await expect(row).toContainText("BUY");
  await expect
    .poll(() =>
      page.evaluate((id) => Reflect.get(window, "__sseBytes").includes(`id: ${id}\n`), buyId),
    )
    .toBeTruthy();
  await request.post(`${fixture}/__fixture/disconnect-streams`);
  await request.post(`${fixture}/__fixture/signals/SELL`);
  await request.post(`${fixture}/__fixture/signals/WAIT`);
  await expect.poll(() => cursors.includes(buyId)).toBeTruthy();
  // Replay contains SELL, but it is historical, not the currently applicable state.
  await expect(row).toContainText("WAIT");
  await expect(row).not.toContainText("SELL");
  await request.post(`${fixture}/__fixture/expire-events`);
  await request.post(`${fixture}/__fixture/signals/UNAVAILABLE`);
  await request.post(`${fixture}/__fixture/disconnect-streams`);
  await expect(page.getByRole("status").filter({ hasText: "信号游标已重置" })).toBeVisible();
  await expect(row).toContainText("数据不可用");
  await expect(row).not.toContainText("BUY");
  await expect(row).not.toContainText("SELL");
});
