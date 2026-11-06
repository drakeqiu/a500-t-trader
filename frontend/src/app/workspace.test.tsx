import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/a500-client", () => ({
  configurationReady: false,
  confirmPreview: vi.fn(),
  discover: vi.fn(),
  getNav: vi.fn(),
  getSignals: vi.fn(),
  pollDeviceToken: vi.fn(),
  requestDeviceCode: vi.fn(),
  streamSignals: vi.fn(),
  uploadPreview: vi.fn(),
}));

import Workspace from "@/app/workspace";

afterEach(() => cleanup());

describe("Workspace authorization entry", () => {
  it("explains missing service configuration and prevents an unusable connection", () => {
    render(<Workspace />);

    expect(screen.getByRole("heading", { level: 1, name: /连接你的\s*投资工作台/ })).toBeTruthy();
    expect(screen.getByRole("status").textContent).toContain(
      "需配置 NEXT_PUBLIC_A500_API_BASE_URL 与 NEXT_PUBLIC_A500_OIDC_ISSUER 后连接数据服务。",
    );
    expect((screen.getByRole("button", { name: "安全连接" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });
});
