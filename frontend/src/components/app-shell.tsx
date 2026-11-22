"use client";

import { useEffect, type ReactNode } from "react";
import Link from "next/link";
import { beginSilentAuthorization, configurationReady, silentAuthorizationKey } from "@/lib/oidc";
import { clearAccessToken } from "@/lib/session";

const returnKey = "a500.auth.return";
const navigation = [
  { href: "/signals", label: "策略信号" },
  { href: "/assets", label: "资产管理" },
  { href: "/data/trades", label: "成交导入" },
  { href: "/data/history", label: "历史行情" },
];

export function AppShell({
  children,
  activePath,
  connected,
  onConnect,
}: {
  children: ReactNode;
  activePath: string;
  connected: boolean;
  onConnect: () => void;
}) {
  useEffect(() => {
    if (connected || !configurationReady || sessionStorage.getItem(silentAuthorizationKey)) return;
    sessionStorage.setItem(silentAuthorizationKey, "1");
    sessionStorage.setItem(returnKey, `${window.location.pathname}${window.location.search}`);
    void beginSilentAuthorization()
      .then((url) => window.location.assign(url))
      .catch(() => {
        sessionStorage.removeItem(silentAuthorizationKey);
        sessionStorage.removeItem(returnKey);
      });
  }, [connected]);

  const connectManually = () => {
    sessionStorage.removeItem(silentAuthorizationKey);
    onConnect();
  };

  const disconnect = () => {
    sessionStorage.setItem(silentAuthorizationKey, "1");
    clearAccessToken();
  };

  return (
    <div className="app-frame">
      <header className="app-topbar">
        <Link className="app-brand" href="/signals" aria-label="A500 Trader 策略信号">
          <span className="brand-monogram">A</span>
          <span>
            A500 <b>研究工作台</b>
          </span>
        </Link>
        <nav className="app-nav" aria-label="主导航">
          {navigation.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              aria-current={activePath === item.href ? "page" : undefined}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <div className="app-session">
          <span
            className={`session-indicator${connected ? " is-connected" : ""}`}
            aria-hidden="true"
          />
          <span>{connected ? "身份已连接" : "尚未连接"}</span>
          {connected ? (
            <Link className="session-action" href="/signals" onClick={disconnect}>
              断开
            </Link>
          ) : (
            <button className="session-action" onClick={connectManually}>
              安全连接
            </button>
          )}
        </div>
      </header>
      <main className="app-main">{children}</main>
      <footer className="app-footnote">
        策略信号仅作辅助决策参考；所有交易由用户在券商软件中自行判断和执行。
      </footer>
    </div>
  );
}
