/**
 * Global test setup - runs once before all tests
 *
 * SQLite-based E2E isolation: Creates per-worker SQLite databases
 * in a temp directory. No Postgres schemas needed.
 *
 * See: docs/LIGHTWEIGHT-OSS-ONBOARDING.md
 */

import path from 'path';
import fs from 'fs';
import os from 'os';
import net from 'net';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Load .env from repo root for consistent configuration
function loadDotEnv(filePath) {
  if (!fs.existsSync(filePath)) return;
  const envContent = fs.readFileSync(filePath, 'utf8');
  for (const rawLine of envContent.split('\n')) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const idx = line.indexOf('=');
    if (idx <= 0) continue;
    const key = line.slice(0, idx).trim();
    let value = line.slice(idx + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (process.env[key] === undefined) {
      process.env[key] = value;
    }
  }
}

// Walk up to find .env
let dir = __dirname;
for (let i = 0; i < 8; i++) {
  const candidate = path.join(dir, '.env');
  if (fs.existsSync(candidate)) {
    loadDotEnv(candidate);
    break;
  }
  dir = path.dirname(dir);
}

async function globalSetup(config) {
  // Set environment variables for test isolation
  process.env.NODE_ENV = 'test';
  process.env.TESTING = '1';

  // Calculate worker count (must match playwright.config.js logic)
  const resolvedWorkers = typeof config?.workers === 'number' ? config.workers : undefined;
  const envWorkers = Number.parseInt(process.env.PLAYWRIGHT_WORKERS ?? "", 10);
  const defaultLocalWorkers = 4;
  const defaultCIWorkers = 4;
  const workers = Number.isFinite(resolvedWorkers) && resolvedWorkers > 0
    ? resolvedWorkers
    : (Number.isFinite(envWorkers) && envWorkers > 0
        ? envWorkers
        : (process.env.CI ? defaultCIWorkers : defaultLocalWorkers));

  // Create temp directory for E2E SQLite databases
  const e2eDbDir = path.join(os.tmpdir(), 'zerg_e2e_dbs');

  const backendPort = Number.parseInt(process.env.BACKEND_PORT ?? "", 10);
  const backendRunning = await new Promise((resolve) => {
    if (!Number.isFinite(backendPort) || backendPort <= 0) {
      resolve(false);
      return;
    }
    const socket = net.createConnection({ host: '127.0.0.1', port: backendPort }, () => {
      socket.destroy();
      resolve(true);
    });
    socket.on('error', () => {
      socket.destroy();
      resolve(false);
    });
  });

  // Clean slate - remove any stale databases from previous runs
  // Guard against deleting live DBs if the backend already started.
  if (!backendRunning && fs.existsSync(e2eDbDir)) {
    fs.rmSync(e2eDbDir, { recursive: true, force: true });
  }
  fs.mkdirSync(e2eDbDir, { recursive: true });

  // Store the temp dir path for spawn-test-backend.js to use
  process.env.E2E_DB_DIR = e2eDbDir;

  if (backendRunning) {
    console.log(`E2E setup: Backend already running on ${backendPort}; skipped DB dir cleanup.`);
  } else {
    console.log(`E2E setup: Created ${e2eDbDir} for ${workers} workers (SQLite per-worker)`);
  }
}

export default globalSetup;
