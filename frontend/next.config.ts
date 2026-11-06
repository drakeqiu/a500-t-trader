import type { NextConfig } from "next";

const apiUpstream = (process.env.A500_INTERNAL_API_URL ?? "http://127.0.0.1:8080").replace(
  /\/$/,
  "",
);

const nextConfig: NextConfig = {
  output: "standalone",
  distDir: process.env.A500_NEXT_DIST_DIR || ".next",
  // Next compression buffers small SSE records instead of flushing them.
  compress: false,
  experimental: {
    proxyTimeout: 3_600_000,
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiUpstream}/:path*`,
      },
    ];
  },
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    root: process.cwd(),
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
