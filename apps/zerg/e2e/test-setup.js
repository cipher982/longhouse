/**
 * Global test setup - runs once before all tests
 *
 * Pre-creates Postgres schemas for all Playwright workers.
 * This ensures schemas exist before tests run, eliminating race conditions
 * from lazy schema creation during test execution.
 *
 * See: docs/work/e2e-test-infrastructure-redesign.md
 */

import { spawn, execSync } from 'child_process';
import path from 'path';
import fs from 'fs';
import os from 'os';
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
  // Keep in sync with playwright.config.js.
  const envWorkers = Number.parseInt(process.env.PLAYWRIGHT_WORKERS ?? "", 10);
  const defaultLocalWorkers = 16;
  const defaultCIWorkers = 4;
  const workers = Number.isFinite(envWorkers) && envWorkers > 0
    ? envWorkers
    : (process.env.CI ? defaultCIWorkers : defaultLocalWorkers);

  // Pre-create one schema per Playwright worker id (0..workers-1).
  // Other tests may use custom non-numeric worker IDs (e.g. guardrail_a) which are
  // created lazily by the backend when first requested.
  const schemaCount = workers;

  // Quiet setup - only show count
  process.stdout.write(`Setting up ${schemaCount} schemas for ${workers} workers... `);

  try {
    // Use uv run python to ensure correct venv with all deps
    // (system python may not have SQLAlchemy, psycopg, etc.)
    const backendDir = path.resolve(__dirname, '../backend');

    // Call Python to drop stale schemas and pre-create fresh ones
    // This ensures all schemas exist before any tests run
    const cleanup = spawn('uv', ['run', 'python', '-c', `
import os
import sys
# TESTING=1 bypasses validation that requires OPENAI_API_KEY etc.
os.environ['TESTING'] = '1'
os.environ['E2E_USE_POSTGRES_SCHEMAS'] = '1'

from zerg.database import default_engine
from zerg.e2e_schema_manager import drop_all_e2e_schemas, ensure_worker_schema

# Clean slate - drop any stale schemas from previous runs
dropped = drop_all_e2e_schemas(default_engine)
if dropped > 0:
    print(f"  Dropped {dropped} stale schemas", file=sys.stderr)

# Pre-create one schema per Playwright worker id (0..workers-1).
for i in range(${schemaCount}):
    ensure_worker_schema(default_engine, str(i))

print(f"  {${schemaCount}} worker schemas ready")
    `], {
      cwd: backendDir,
      stdio: 'inherit',
      env: { ...process.env, E2E_USE_POSTGRES_SCHEMAS: '1', TESTING: '1' }
    });

    await new Promise((resolve, reject) => {
      cleanup.on('close', (code) => {
        if (code === 0) {
          resolve();
        } else {
          reject(new Error(`Schema setup failed with code ${code}`));
        }
      });
    });
  } catch (error) {
    // Fail fast - if schemas can't be created, tests will definitely fail
    console.log('FAILED');
    console.error('Schema pre-creation failed:', error.message);
    throw error;
  }

  console.log('done');
}

export default globalSetup;
