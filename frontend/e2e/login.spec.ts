import { expect, test } from "@playwright/test";

test("shows configuration guidance and disables connection until configured", async ({ page }) => {
  await page.goto("/");

  await expect(
    page.getByRole("heading", { level: 1, name: /连接你的\s*投资工作台/ }),
  ).toBeVisible();
  await expect(page.getByRole("status")).toContainText("NEXT_PUBLIC_A500_API_BASE_URL");
  await expect(page.getByRole("status")).toContainText("NEXT_PUBLIC_A500_OIDC_ISSUER");
  await expect(page.getByRole("button", { name: "安全连接" })).toBeDisabled();
});
