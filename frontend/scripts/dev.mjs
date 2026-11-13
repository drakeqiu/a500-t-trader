import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoDir = path.resolve(frontendDir, "..");
const webPort = Number(process.env.DEV_WEB_PORT || 3000);
const nextPort = Number(process.env.DEV_NEXT_PORT || webPort + 1);
const fixturePort = Number(process.env.DEV_FIXTURE_PORT || 38100);
const origin = `http://localhost:${webPort}`;
const children = [];
let stopping = false;

function start(command, args, cwd, env = process.env) {
  const child = spawn(command, args, { cwd, env, stdio: "inherit" });
  children.push(child);
  child.on("error", (error) => {
    console.error(`Failed to start ${command}: ${error.message}`);
    shutdown(1);
  });
  child.on("exit", (code, signal) => {
    if (!stopping) {
      console.error(`${command} exited (${signal || code})`);
      shutdown(code || 1);
    }
  });
  return child;
}

function shutdown(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const child of children) {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
  }
  process.exitCode = code;
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => shutdown(0));
}

const fixtureEnv = {
  ...process.env,
  FIXTURE_DYNAMIC_ORIGIN: "1",
  FIXTURE_AUTO_SUBJECT: process.env.DEV_MOCK_SUBJECT || "local-demo",
  FIXTURE_DEMO_DATA: "1",
};
start(
  "uv",
  ["run", "python", "scripts/frontend-api-mock.py", "--test-mode", "--port", String(fixturePort)],
  repoDir,
  fixtureEnv,
);
start(
  "bun",
  ["x", "next", "dev", "--hostname", "127.0.0.1", "--port", String(nextPort)],
  frontendDir,
  {
    ...process.env,
    NEXT_PUBLIC_OIDC_ISSUER: "/fixture-idp",
    NEXT_PUBLIC_OIDC_CLIENT_ID: "a500-browser-client",
  },
);
start(
  process.execPath,
  [
    path.join(frontendDir, "e2e/gateway.mjs"),
    String(webPort),
    String(nextPort),
    String(fixturePort),
    "0.0.0.0",
  ],
  frontendDir,
  process.env,
);

console.log(`Mock frontend: ${origin}`);
console.log("Click ‘安全连接’ to enter the mock session; no Rauthy or business API is used.");
