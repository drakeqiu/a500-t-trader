"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { completeAuthorization } from "@/lib/oidc";
import { setAccessToken } from "@/lib/session";

const returnKey = "a500.auth.return";
const allowedRoutes = new Set(["/signals", "/assets", "/data/trades", "/data/history"]);

function consumeReturnPath() {
  const saved = sessionStorage.getItem(returnKey);
  sessionStorage.removeItem(returnKey);
  if (!saved) return "/signals";
  try {
    const url = new URL(saved, window.location.origin);
    return url.origin === window.location.origin && allowedRoutes.has(url.pathname)
      ? `${url.pathname}${url.search}${url.hash}`
      : "/signals";
  } catch {
    return "/signals";
  }
}

export default function Home() {
  const router = useRouter();
  const started = useRef(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const pending = completeAuthorization();
    if (!pending) {
      sessionStorage.removeItem(returnKey);
      router.replace("/signals");
      return;
    }
    void pending
      .then(({ token, expiresIn }) => {
        setAccessToken(token, expiresIn);
        router.replace(consumeReturnPath());
      })
      .catch((reason: unknown) => {
        sessionStorage.removeItem(returnKey);
        setError(reason instanceof Error ? reason.message : "无法完成身份授权，请重新连接。");
      });
  }, [router]);

  if (error) {
    return (
      <main>
        <h1>无法完成安全连接</h1>
        <p role="alert">{error}</p>
        <Link href="/signals">返回策略信号</Link>
      </main>
    );
  }
  return <main aria-busy="true">正在完成安全连接…</main>;
}
