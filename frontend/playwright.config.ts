import { existsSync } from "node:fs";
import { defineConfig, devices } from "@playwright/test";

const fixturePort = process.env.A500_E2E_FIXTURE_PORT || "38100";
const webPort = process.env.A500_E2E_WEB_PORT || "3100";
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
      command: `uv run python scripts/frontend-integration-fixture.py --test-mode --port ${fixturePort}`,
      cwd: "..",
      url: `${fixtureOrigin}/__fixture/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `bun run dev --hostname 127.0.0.1 --port ${webPort}`,
      url: `http://127.0.0.1:${webPort}`,
      env: {
        NEXT_PUBLIC_A500_API_BASE_URL: "/api",
        NEXT_PUBLIC_A500_OIDC_ISSUER: `${fixtureOrigin}/fixture-idp`,
        NEXT_PUBLIC_A500_OIDC_CLIENT_ID: "a500-device-client",
        A500_INTERNAL_API_URL: fixtureOrigin,
        A500_NEXT_DIST_DIR: `.next-e2e-${webPort}`,
        A500_E2E_FIXTURE_PORT: fixturePort,
      },
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
