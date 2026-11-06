import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "A500 Trader · 实时信号",
  description: "A500 ETF 策略信号与账户工作台。",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
