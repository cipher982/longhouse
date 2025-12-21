import fs from "fs";
import os from "os";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function findDotEnv(startDir) {
  let dir = startDir;
  for (let i = 0; i < 8; i++) {
    const candidate = path.join(dir, ".env");
    if (fs.existsSync(candidate)) return candidate;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

function readEnvVarFromFile(envPath, key, fallback) {
  if (!envPath) return fallback;
  const content = fs.readFileSync(envPath, "utf8");
  for (const rawLine of content.split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const idx = line.indexOf("=");
    if (idx <= 0) continue;
    const k = line.slice(0, idx).trim();
    if (k !== key) continue;
    let v = line.slice(idx + 1).trim();
    v = v.replace(/\s+#.*$/, "").trim();
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1);
    return v || fallback;
  }
  return fallback;
}

const envPath = findDotEnv(__dirname);
const BACKEND_PORT = Number.parseInt(process.env.BACKEND_PORT ?? readEnvVarFromFile(envPath, "BACKEND_PORT", "8001"), 10);

const workers = process.env.CI ? 4 : os.cpus().length;

export default {
  testDir: "./probes",
  testMatch: ["**/*backend_parallelism.probe.spec.ts"],
  fullyParallel: true,
  workers,
  retries: 0,
  timeout: 30_000,
  reporter: [
    ["line"],
    ["json", { outputFile: "test-results/backend-probe-timeline.json" }],
  ],
  webServer: [
    {
      command: `BACKEND_PORT=${BACKEND_PORT} node spawn-test-backend.js`,
      port: BACKEND_PORT,
      cwd: __dirname,
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
};
