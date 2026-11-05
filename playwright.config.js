import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  use: {
    baseURL: "http://127.0.0.1:8765",
    browserName: "chromium",
  },
  webServer: {
    command: "uv run python tests/browser/server.py",
    url: "http://127.0.0.1:8765/healthz",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
    env: {
      A500_OIDC_ISSUER: "https://identity.example.test",
      A500_OIDC_CLIENT_ID: "a500-web-client",
      A500_OIDC_AUDIENCE: "a500-api",
    },
  },
});
