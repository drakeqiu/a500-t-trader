import { randomUUID } from "node:crypto";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

// Business REST/SSE calls use deterministic in-memory API mocks and fixture identity.
// This suite exercises browser workflows, not live Keycloak or market-provider access.
const fixture = `http://127.0.0.1:${process.env.A500_E2E_FIXTURE_PORT || "38100"}`;

async function connect(page: Page, subject: string, heading = "策略信号") {
  const issuedToken = page.waitForResponse(
    (response) => response.url() === `${fixture}/fixture-idp/token` && response.ok(),
  );
  await page.getByRole("button", { name: "安全连接", exact: true }).first().click();
  await page.waitForURL(`${fixture}/fixture-idp/authorize**`);
  const authorization = new URL(page.url());
  expect(authorization.searchParams.get("client_id")).toBe("a500-browser-client");
  expect(authorization.searchParams.get("redirect_uri")).toBe(
    `${new URL(test.info().project.use.baseURL!).origin}/`,
  );
  expect(authorization.searchParams.get("code_challenge_method")).toBe("S256");
  expect(authorization.searchParams.get("state")).toBeTruthy();
  expect(authorization.searchParams.get("code_challenge")).toBeTruthy();
  await page.getByLabel("Fixture account").fill(subject);
  await page.getByRole("button", { name: "Authorize fixture account" }).click();
  const token = (await (await issuedToken).json()).access_token as string;
  await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
  expect(new URL(page.url()).search).toBe("");
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
  await page.getByRole("button", { name: /^(创建|保存)账户快照$/, exact: true }).click();
  const response = await saved;
  expect(response.status()).toBe(status);
  return response;
}

async function upload(page: Page, request: APIRequestContext, kind: "valid" | "invalid") {
  const csv = await request.get(`${fixture}/__fixture/csv/${kind}`);
  expect(csv.ok()).toBeTruthy();
  await page.locator('input[type="file"][aria-label="选择成交文件"]').setInputFiles({
    name: `${kind}.csv`,
    mimeType: "text/csv",
    buffer: await csv.body(),
  });
  const preview = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/trade-imports/previews") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "上传并预览", exact: true }).click();
  const response = await preview;
  expect(response.status()).toBe(200);
  return response.json() as Promise<{ batch_id: string; errors: number; new: number }>;
}

test("opens the signals route by default and serves the API from one origin", async ({
  page,
  request,
}) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/signals$/);
  await expect(page.getByRole("heading", { name: "策略信号", exact: true })).toBeVisible();
  await expect(
    page.getByRole("banner").getByRole("button", { name: "安全连接", exact: true }),
  ).toBeEnabled();
  expect(new URL(page.url()).origin).toBe(new URL(test.info().project.use.baseURL!).origin);
  expect((await request.get("/api/v1/account-snapshot")).status()).toBe(401);
  expect((await request.get("/v1/account-snapshot")).status()).toBe(404);
});

test("navigates all four business routes without losing their page identity", async ({ page }) => {
  await page.goto("/");
  for (const route of [
    { label: "资产管理", path: "/assets", heading: "账户资产" },
    { label: "成交导入", path: "/data/trades", heading: "成交导入" },
    { label: "历史行情", path: "/data/history", heading: "历史行情" },
    { label: "策略信号", path: "/signals", heading: "策略信号" },
  ]) {
    await page
      .getByRole("navigation", { name: "主导航" })
      .getByRole("link", { name: route.label })
      .click();
    await expect(page).toHaveURL(new RegExp(`${route.path.replaceAll("/", "\\/")}$`));
    await expect(page.getByRole("heading", { name: route.heading, exact: true })).toBeVisible();
  }
});

test("returns to a protected route after fixture authorization and disconnects the session", async ({
  page,
}) => {
  await page.goto("/data/history");
  await expect(page.getByRole("heading", { name: "历史行情", exact: true })).toBeVisible();
  await connect(page, `history-return-${randomUUID()}`, "历史行情");
  await expect(page).toHaveURL(/\/data\/history$/);
  await expect(page.getByRole("link", { name: "断开" })).toBeVisible();
  await page.getByRole("link", { name: "断开" }).click();
  await expect(page).toHaveURL(/\/signals$/);
  await expect(page.getByRole("banner").getByText("尚未连接", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("banner").getByRole("button", { name: "安全连接", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "策略信号", exact: true })).toBeVisible();
});

test("rejects unsolicited callbacks and removes authorization data from the address bar", async ({
  page,
}) => {
  let tokenRequests = 0;
  page.on("request", (request) => {
    if (request.url() === `${fixture}/fixture-idp/token`) tokenRequests++;
  });
  await page.goto("/?code=untrusted&state=unexpected");
  await expect(page.getByText("授权会话无效或已过期，请重新连接。")).toBeVisible();
  await expect(page).toHaveURL("/");
  expect(tokenRequests).toBe(0);
});

test("signs in when SubtleCrypto is unavailable on HTTP", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window.crypto, "subtle", { value: undefined, configurable: true });
  });
  await page.goto("/");
  const token = await connect(page, `http-${randomUUID()}`);
  expect(token).toBeTruthy();
});

test("imports trades from /data/trades after reviewing the mocked preview", async ({
  page,
  request,
}) => {
  await page.goto("/data/trades");
  const subject = `trades-page-${randomUUID()}`;
  const token = await connect(page, subject, "成交导入");
  const importRegion = page.getByRole("main");
  const uploadOnTradesPage = async (kind: "valid" | "invalid") => {
    const csv = await request.get(`${fixture}/__fixture/csv/${kind}`);
    await importRegion.locator('input[type="file"][aria-label="选择成交文件"]').setInputFiles({
      name: `${kind}.csv`,
      mimeType: "text/csv",
      buffer: await csv.body(),
    });
    const response = page.waitForResponse(
      (item) =>
        item.url().endsWith("/api/v1/trade-imports/previews") && item.request().method() === "POST",
    );
    await importRegion.getByRole("button", { name: "上传并预览", exact: true }).click();
    expect((await response).status()).toBe(200);
  };
  await expect(importRegion.getByText("当前成交", { exact: true })).toBeVisible();
  await importRegion.getByLabel("历史成交").check();
  await uploadOnTradesPage("invalid");
  await expect(importRegion.getByRole("list", { name: "导入逐行错误" })).toContainText("第 3 行");
  await expect(importRegion.getByRole("button", { name: "确认导入", exact: true })).toHaveCount(0);

  await uploadOnTradesPage("valid");
  await expect(importRegion.getByRole("heading", { name: "检查导入预览" })).toBeVisible();
  await expect(importRegion.getByText("历史成交", { exact: true })).toBeVisible();
  const confirmation = page.waitForResponse(
    (response) =>
      response.url().includes("/api/v1/trade-imports/previews/") &&
      response.url().endsWith("/confirm") &&
      response.request().method() === "POST",
  );
  await importRegion.getByRole("button", { name: "确认导入", exact: true }).click();
  expect((await confirmation).status()).toBe(200);
  await expect(importRegion.getByRole("heading", { name: "已确认", exact: true })).toBeVisible();
  await expect(importRegion.getByText(/已确认 1 条新增记录/)).toBeVisible();
  expect(token).toBeTruthy();
});

test("authenticates on /assets and creates an account snapshot through the API", async ({
  page,
}) => {
  await page.goto("/assets");
  const subject = `assets-${randomUUID()}`;
  const navRead = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/account-nav") && response.request().method() === "GET",
  );
  await connect(page, subject, "账户资产");
  const navResponse = await navRead;
  expect([200, 404]).toContain(navResponse.status());
  expect(["available", "unavailable"]).toContain((await navResponse.json()).status);

  await page.getByRole("button", { name: "创建快照", exact: true }).click();
  await fillSnapshot(page);
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/account-snapshot") && response.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "创建账户快照", exact: true }).click();
  expect((await saved).status()).toBe(201);
  await expect(
    page.getByText("账户快照已保存。正在从服务端重新读取完整净资产估值。"),
  ).toBeVisible();
  await expect(page.getByText("¥125.50")).toBeVisible();
});

test("validates snapshot amounts and preserves oversized decimal precision on repeat submits", async ({
  page,
}) => {
  await page.goto("/assets");
  await connect(page, `assets-precision-${randomUUID()}`, "账户资产");
  await page.getByRole("button", { name: "创建快照", exact: true }).click();
  const large = `9${"0".repeat(80)}.000000000000000001`;
  await fillSnapshot(page, "NaN");
  let snapshotWrites = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/api/v1/account-snapshot") && request.method() === "PUT") {
      snapshotWrites++;
    }
  });
  await page.getByRole("button", { name: "创建账户快照", exact: true }).click();
  await expect(page.getByRole("region", { name: "账户快照" }).getByRole("alert")).toContainText(
    "可用现金",
  );
  expect(snapshotWrites).toBe(0);

  await page.getByLabel("可用现金", { exact: true }).fill("");
  await page.getByRole("button", { name: "创建账户快照", exact: true }).click();
  expect(snapshotWrites).toBe(0);

  await page.getByLabel("可用现金", { exact: true }).fill(large);
  await page.getByLabel("持仓数量（JSON）").fill("{}");
  await page.getByLabel("保护底仓（JSON）").fill("{}");
  await page.getByLabel("可卖数量（JSON）").fill("{}");
  await page.getByLabel("成本基础（JSON）").fill("{}");
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/account-snapshot") && response.request().method() === "PUT",
  );
  await page.getByLabel("快照时间（含时区）").evaluate((input: HTMLInputElement) => {
    input.form?.requestSubmit();
    input.form?.requestSubmit();
  });
  const response = await saved;
  expect(response.status()).toBe(201);
  expect(snapshotWrites).toBe(1);
  expect((await response.json()).available_cash).toBe(large);
  await expect(
    page.getByRole("region", { name: "账户快照" }).getByText(`¥${large}`, { exact: true }),
  ).toBeVisible();
});

test("creates and edits an account; a mocked 412 retains the draft until explicitly replaced", async ({
  page,
  context,
  request,
}) => {
  const unauthorized = await request.get("/api/v1/account-snapshot");
  expect(unauthorized.status()).toBe(401);
  await page.goto("/assets");
  const subject = `snapshot-${randomUUID()}`;
  const token = await connect(page, subject, "账户资产");
  const nav = page.getByRole("region", { name: "当前净资产（NAV）" });
  await expect(nav).toContainText(/不可用|不可估值|暂无|尚无/);
  await page.getByRole("button", { name: "创建快照", exact: true }).click();
  await fillSnapshot(page);
  const created = await saveSnapshot(page, 201);
  expect(created.headers().etag).toBeTruthy();
  expect(created.request().headers()["if-none-match"]).toBe("*");
  await expect(nav).toContainText("market_data_unavailable");
  await expect(nav.locator(".nav-summary")).toHaveCount(0);

  const other = await context.newPage();
  await other.goto("/assets");
  await connect(other, subject, "账户资产");
  await other.getByRole("button", { name: "编辑快照", exact: true }).click();
  await expect(other.getByLabel("可用现金", { exact: true })).toHaveValue("125.50");
  await page.getByRole("button", { name: "编辑快照", exact: true }).click();
  await page.getByLabel("可用现金", { exact: true }).fill("999.00");
  await other.getByLabel("可用现金", { exact: true }).fill("456.00");
  const edited = await saveSnapshot(other, 200);
  expect(edited.headers().etag).not.toBe(created.headers().etag);

  const conflict = await saveSnapshot(page, 412);
  expect(conflict.request().headers()["if-match"]).toBe(created.headers().etag);
  await expect(page.getByRole("alert").filter({ hasText: "草稿仍保留" })).toBeVisible();
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
  await expect(
    page.getByRole("region", { name: "账户快照" }).getByText("¥456.00", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "编辑快照", exact: true }).click();
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
  await page.goto("/assets");
  const subject = `import-${randomUUID()}`;
  let token = await connect(page, subject, "账户资产");
  await page.getByRole("button", { name: "创建快照", exact: true }).click();
  await fillSnapshot(page);
  await saveSnapshot(page, 201);
  const before = await request.get("/api/v1/account-snapshot", {
    headers: { Authorization: `Bearer ${token}` },
  });
  const originalSnapshot = await before.json();
  await page
    .getByRole("navigation", { name: "主导航" })
    .getByRole("link", { name: "成交导入" })
    .click();
  await expect(page.getByRole("heading", { name: "成交导入", exact: true })).toBeVisible();
  const imports = page.getByRole("main");
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
  await expect(
    page.getByRole("banner").getByRole("button", { name: "安全连接", exact: true }),
  ).toBeEnabled();
  const restored = page.waitForResponse((response) =>
    response.url().endsWith(`/api/v1/trade-imports/previews/${valid.batch_id}`),
  );
  token = await connect(page, subject, "成交导入");
  expect((await restored).status()).toBe(200);
  await expect(imports).toContainText(valid.batch_id);
  await expect(
    page.getByRole("status").filter({ hasText: "已恢复此账户最近保存的成交批次" }),
  ).toBeVisible();
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
  await connect(page, subject, "成交导入");
  await expect(imports).toContainText(valid.batch_id);
  await expect(imports.getByRole("heading", { name: "已确认", exact: true })).toBeVisible();
  const duplicate = await upload(page, request, "valid");
  expect(duplicate.new).toBe(0);
  const duplicateCount = imports.locator("dl > div").filter({
    has: page.locator("dt", { hasText: /^重复记录$/ }),
  });
  await expect(duplicateCount.locator("dd")).toHaveText("1");
});

test("consumes mocked SSE and resumes its cursor; expired history resets to current mock state", async ({
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
  await expect(row).toContainText("等待信号");
  await expect(page.getByRole("region", { name: "策略信号" })).toContainText("实时订阅");
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
  await expect(row).toContainText("等待信号");
  await expect(row).not.toContainText("SELL");
  await request.post(`${fixture}/__fixture/expire-events`);
  await request.post(`${fixture}/__fixture/signals/UNAVAILABLE`);
  await request.post(`${fixture}/__fixture/disconnect-streams`);
  await expect(row).toContainText("数据不可用");
  await expect(row).not.toContainText("BUY");
  await expect(row).not.toContainText("SELL");
});
test("reflows signal rows on phones and contains table scrolling at intermediate widths", async ({
  page,
  request,
}) => {
  await request.post(`${fixture}/__fixture/signals/WAIT`);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/signals");
  await connect(page, `layout-${randomUUID()}`, "策略信号");

  const table = page.getByRole("table");
  await expect(table).toBeVisible();
  const row = page.getByRole("row").filter({
    has: page.getByRole("rowheader", { name: "563360" }),
  });
  await expect(row).toContainText("等待信号");
  await expect(row).toContainText("非下单指令");

  const measure = () =>
    page.locator(".signals-table-scroll").evaluate((frame) => {
      const cells = Array.from(frame.querySelector("tbody tr")!.children);
      const bounds = cells.map((cell) => {
        const rect = cell.getBoundingClientRect();
        return { left: rect.left, top: rect.top, right: rect.right };
      });
      return {
        viewport: window.innerWidth,
        document: document.documentElement.scrollWidth,
        frame: frame.clientWidth,
        table: frame.scrollWidth,
        bounds,
      };
    });

  const phone = await measure();
  expect(phone.document).toBeLessThanOrEqual(phone.viewport);
  expect(phone.table).toBeLessThanOrEqual(phone.frame);
  expect(Math.abs(phone.bounds[0]!.top - phone.bounds[1]!.top)).toBeLessThan(2);
  expect(Math.abs(phone.bounds[2]!.top - phone.bounds[5]!.top)).toBeLessThan(2);
  expect(phone.bounds[3]!.top).toBeGreaterThan(phone.bounds[2]!.top);

  await page.setViewportSize({ width: 768, height: 900 });
  const tablet = await measure();
  expect(tablet.document).toBeLessThanOrEqual(tablet.viewport);
  expect(tablet.table).toBeGreaterThan(tablet.frame);

  await page.setViewportSize({ width: 1440, height: 1000 });
  const desktop = await measure();
  expect(desktop.document).toBeLessThanOrEqual(desktop.viewport);
  expect(desktop.table).toBeLessThanOrEqual(desktop.frame);
});
test("creates a history task without treating acceptance as completion", async ({ page }) => {
  await page.goto("/");
  await connect(page, "history-owner");
  await page.getByRole("link", { name: "历史行情" }).click();
  await expect(page.getByRole("heading", { name: "历史行情", exact: true })).toBeVisible();

  const created = {
    type: "daily_history",
    id: "00000000-0000-4000-8000-000000000001",
    symbol: "SH.510500",
    start_date: "2025-01-01",
    end_date: "2025-01-02",
    status: "queued",
    stage: "queued",
    cancel_requested: false,
    total_chunks: 0,
    completed_chunks: 0,
    created_at: "2025-01-03T00:00:00+00:00",
    updated_at: "2025-01-03T00:00:00+00:00",
    finished_at: null,
    error: null,
  };
  await page.route("**/api/v1/jobs", async (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({
      type: "daily_history",
      symbol: "SH.510500",
      start_date: expect.any(String),
      end_date: expect.any(String),
    });
    await route.fulfill({ status: 202, json: created });
  });

  await page.getByLabel("证券代码").fill("SH.510500");
  await page.getByLabel("开始日期").fill("2025-01-01");
  await page.getByLabel("结束日期").fill("2025-01-02");
  await page.getByRole("button", { name: "创建任务" }).click();
  await expect(page.getByText("等待执行")).toBeVisible();
  await expect(page.getByText("提交成功不代表行情已拉取完成。")).toBeVisible();
  await expect(page.getByText("已完成", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("progressbar")).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  const pageBounds = await page
    .locator(".history-page")
    .evaluate((element) => element.getBoundingClientRect().right);
  expect(pageBounds).toBeLessThanOrEqual(391);
  await expect(page.getByRole("heading", { name: "历史行情", exact: true })).toBeVisible();
});
