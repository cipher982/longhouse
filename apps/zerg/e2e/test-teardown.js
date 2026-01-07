/**
 * Global test teardown - runs once after all tests complete
 * Modern testing practices 2025: Automatic cleanup of test artifacts
 */

import { spawn, execSync } from 'child_process';
import path from 'path';
import fs from 'fs';
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

async function globalTeardown(config) {
  // Quiet cleanup - suppress output by default
  // Teardown is best-effort: globalSetup will clean stale schemas anyway
  try {
    const backendDir = path.resolve(__dirname, '../backend');

    // Call Python cleanup script to remove all test databases/schemas
    // Use a short timeout - if it hangs, globalSetup will clean up next run
    const cleanup = spawn('uv', ['run', 'python', '-c', `
import os
import sys
os.environ['TESTING'] = '1'
os.environ['E2E_USE_POSTGRES_SCHEMAS'] = '1'

try:
    from zerg.e2e_schema_manager import drop_all_e2e_schemas
    from zerg.database import default_engine
    if default_engine:
        drop_all_e2e_schemas(default_engine)
except Exception as e:
    # Best-effort cleanup - globalSetup will handle stale schemas
    print(f"Teardown warning: {e}", file=sys.stderr)
    sys.exit(0)  # Don't fail on cleanup issues
    `], {
      cwd: backendDir,
      stdio: 'pipe',  // Suppress output
      env: { ...process.env, E2E_USE_POSTGRES_SCHEMAS: '1', TESTING: '1' }
    });

    // Timeout after 30s - don't let cleanup hang indefinitely
    const timeoutId = setTimeout(() => {
      cleanup.kill('SIGTERM');
    }, 30000);

    await new Promise((resolve) => {
      cleanup.on('close', (code) => {
        clearTimeout(timeoutId);
        // Always resolve - cleanup is best-effort
        resolve();
      });
    });

  } catch (error) {
    // Log cleanup failures to stderr but don't fail the test run
    // globalSetup will clean stale schemas on next run
    console.error('Test cleanup warning:', error.message);
  }
}

export default globalTeardown;
