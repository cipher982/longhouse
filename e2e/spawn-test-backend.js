#!/usr/bin/env node

/**
 * Spawn the loopback-only E2E backend in an owned process group.
 *
 * The run root is unique per invocation. This process never loads dotenv,
 * deletes another run's files, or finds a process to kill by port.
 */

import crypto from "crypto";
import { spawn } from "child_process";
import { join } from "path";
import fs from "fs";
import net from "net";
import path from "path";
import { fileURLToPath } from "url";
import {
  ensureTestRuntime,
  parsePort,
  randomPort,
  safeChildEnvironment,
  signalProcessGroup,
  stripAmbientSecrets,
} from "./test-runtime.js";
const suppliedIsolatedRuntime = process.env.LONGHOUSE_TEST_ISOLATED === "1";
const runtime = ensureTestRuntime();
stripAmbientSecrets();

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
function getBackendPort() {
  return suppliedIsolatedRuntime && process.env.E2E_BACKEND_PORT
    ? parsePort(process.env.E2E_BACKEND_PORT, "E2E_BACKEND_PORT")
    : randomPort();
}

const BACKEND_PORT = getBackendPort();
process.env.E2E_BACKEND_PORT = String(BACKEND_PORT);
process.env.BACKEND_PORT = String(BACKEND_PORT);
const backendBaseUrl = `http://127.0.0.1:${BACKEND_PORT}`;

async function isPortOpen(port) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: "127.0.0.1", port }, () => {
      socket.destroy();
      resolve(true);
    });
    socket.once("error", () => {
      socket.destroy();
      resolve(false);
    });
    socket.setTimeout(1000, () => {
      socket.destroy();
      resolve(false);
    });
  });
}

if (await isPortOpen(BACKEND_PORT)) {
  console.error(
    `[spawn-backend] Refusing to reuse backend on port ${BACKEND_PORT}; choose another E2E_BACKEND_PORT.`,
  );
  process.exit(1);
}

const dbPath = path.join(runtime.dbDir, "e2e.db");
const databaseUrl = `sqlite:///${dbPath}`;
const toolStubsPath = join(__dirname, "fixtures", "tool-stubs.json");
const workspacePath = path.join(runtime.root, "workspaces");
const claudeConfigDir = path.join(runtime.root, "claude");
fs.mkdirSync(workspacePath, { recursive: true });
fs.mkdirSync(claudeConfigDir, { recursive: true });

const fernetSecret = crypto
  .randomBytes(32)
  .toString("base64")
  .replaceAll("+", "-")
  .replaceAll("/", "_");
const childEnv = safeChildEnvironment({
  BACKEND_PORT: String(BACKEND_PORT),
  E2E_BACKEND_PORT: String(BACKEND_PORT),
  ENVIRONMENT: "test:e2e",
  TEST_WORKER_ID: "0",
  NODE_ENV: "test",
  TESTING: "1",
  AUTH_DISABLED: "1",
  SINGLE_TENANT: "0",
  DEV_ADMIN: "1",
  ADMIN_EMAILS: "dev@local",
  LLM_TOKEN_STREAM: "true",
  E2E_FAKE_SESSION_CHAT: "1",
  E2E_FAKE_SESSION_MESSAGES: "1",
  E2E_DEFAULT_MODEL: "gpt-scripted",
  LONGHOUSE_WORKSPACE_PATH: workspacePath,
  CLAUDE_CONFIG_DIR: claudeConfigDir,
  E2E_HATCH_PATH: join(__dirname, "bin", "hatch"),
  LONGHOUSE_TOOL_STUBS_PATH: toolStubsPath,
  LONGHOUSE_SEARCH_PROJECTOR_WORKERS: "4",
  LOG_LEVEL: "ERROR",
  APP_PUBLIC_URL: "",
  PUBLIC_SITE_URL: "",
});
// These values are intentionally set after secret stripping: they are owned,
// deterministic fixture inputs, not credentials inherited from the host.
childEnv.APP_PUBLIC_URL = "";
childEnv.PUBLIC_SITE_URL = "";
childEnv.DATABASE_URL = databaseUrl;
childEnv.FERNET_SECRET = fernetSecret;
childEnv.LONGHOUSE_API_URL = backendBaseUrl;
childEnv.PATH = `${join(__dirname, "bin")}:${childEnv.PATH || ""}`;

console.log(
  `[spawn-backend] Starting E2E backend on port ${BACKEND_PORT} with SQLite: ${dbPath}`,
);

const backend = spawn(
  "uv",
  [
    "run",
    "python",
    "-m",
    "uvicorn",
    "zerg.qa.e2e_app:app",
    "--host=127.0.0.1",
    `--port=${BACKEND_PORT}`,
    "--workers=1",
    "--log-level=error",
  ],
  {
    env: childEnv,
    cwd: join(__dirname, "..", "server"),
    stdio: "inherit",
    detached: process.platform !== "win32",
  },
);

let backendClosed = false;
let shuttingDown = false;
let requestedExitCode = 0;
let forceTimer;

function terminateBackend(signal) {
  if (!backend.pid) return;
  try {
    if (
      !signalProcessGroup(backend.pid, signal) &&
      backend.connected !== false
    ) {
      backend.kill(signal);
    }
  } catch (error) {
    if (error?.code !== "ESRCH")
      console.error(
        `[spawn-backend] Failed to signal backend: ${error.message}`,
      );
  }
}

function shutdown(signal, exitCode = 1) {
  if (shuttingDown) return;
  shuttingDown = true;
  requestedExitCode = exitCode;
  console.log(
    `[spawn-backend] Received ${signal}; stopping owned backend process group`,
  );
  terminateBackend(signal);
  forceTimer = setTimeout(() => {
    if (!backendClosed) terminateBackend("SIGKILL");
    process.exit(requestedExitCode);
  }, 5000);
  forceTimer.unref();
}

backend.on("error", (error) => {
  console.error(`[spawn-backend] Backend error: ${error.message}`);
  shutdown("SIGTERM", 1);
});

backend.on("close", (code) => {
  backendClosed = true;
  clearTimeout(forceTimer);
  // A reparented descendant retains the detached process group. Kill that
  // group even when the uvicorn leader has already exited.
  terminateBackend("SIGKILL");
  if (!shuttingDown) requestedExitCode = code ?? 1;
  process.exit(requestedExitCode);
});

process.on("SIGTERM", () => shutdown("SIGTERM", 143));
process.on("SIGINT", () => shutdown("SIGINT", 130));
process.on("exit", () => {
  if (!backendClosed) terminateBackend("SIGKILL");
});

process.stdin.resume();
