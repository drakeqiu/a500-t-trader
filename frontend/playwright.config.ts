import { existsSync } from "node:fs";
import { defineConfig, devices } from "@playwright/test";

// Playwright forces colors in workers; do not pass them conflicting color settings.
if (process.env.NO_COLOR) {
  delete process.env.NO_COLOR;
  process.env.FORCE_COLOR ??= "0";
}

const fixturePort = process.env.E2E_FIXTURE_PORT || "38100";
const webPort = process.env.E2E_WEB_PORT || "3100";
const nextPort = process.env.E2E_NEXT_PORT || String(Number(webPort) + 1);
const fixtureOrigin = `http://127.0.0.1:${fixturePort}`;
const chrome = ["/usr/bin/google-chrome", "/opt/google/chrome/chrome"].find(existsSync);

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: "list",
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    ...(process.env.PLAYWRIGHT_CHANNEL
      ? { channel: process.env.PLAYWRIGHT_CHANNEL }
      : chrome
        ? { launchOptions: { executablePath: chrome } }
        : {}),
    headless: true,
    ...devices["Desktop Chrome"],
  },
  webServer: [
    {
      command: `uv run python scripts/frontend-api-mock.py --test-mode --port ${fixturePort}`,
      cwd: "..",
      url: `${fixtureOrigin}/__fixture/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `bun run dev --hostname 127.0.0.1 --port ${nextPort}`,
      url: `http://127.0.0.1:${nextPort}`,
      env: {
        NEXT_PUBLIC_OIDC_ISSUER: `${fixtureOrigin}/fixture-idp`,
        NEXT_PUBLIC_OIDC_CLIENT_ID: "a500-browser-client",
        NEXT_DIST_DIR: `.next-e2e-${webPort}`,
      },
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `node e2e/gateway.mjs ${webPort} ${nextPort} ${fixturePort}`,
      url: `http://127.0.0.1:${webPort}`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
