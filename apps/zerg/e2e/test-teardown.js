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
  console.log('🧹 Starting test environment cleanup...');

  try {
    // Resolve a Python interpreter ('python' or 'python3')
    const pythonCmd = (() => {
      try { execSync('python --version', { stdio: 'ignore' }); return 'python'; } catch {}
      try { execSync('python3 --version', { stdio: 'ignore' }); return 'python3'; } catch {}
      return null;
    })();

    if (!pythonCmd) {
      throw new Error("No Python interpreter found (python/python3)");
    }

    // Call Python cleanup script to remove all test databases/schemas
    // E2E tests use Postgres schema isolation (set in spawn-test-backend.js)
    const cleanup = spawn(pythonCmd, ['-c', `
import sys
import os
sys.path.insert(0, '${path.resolve('../backend')}')
os.environ['E2E_USE_POSTGRES_SCHEMAS'] = '1'

# Use Postgres schema cleanup for E2E tests
from zerg.e2e_schema_manager import drop_all_e2e_schemas
from zerg.database import default_engine
dropped = drop_all_e2e_schemas(default_engine)
print(f"✅ Dropped {dropped} E2E test schemas")
    `], {
      cwd: path.resolve('../backend'),
      stdio: 'inherit',
      env: { ...process.env, E2E_USE_POSTGRES_SCHEMAS: '1' }
    });
    // If this fails, the environment must expose 'python' on PATH.

    await new Promise((resolve, reject) => {
      cleanup.on('close', (code) => {
        if (code === 0) {
          resolve();
        } else {
          reject(new Error(`Cleanup failed with code ${code}`));
        }
      });
    });

    console.log('✅ Test environment cleanup completed');

  } catch (error) {
    console.error('❌ Test cleanup failed:', error.message);
    // Continue anyway - don't fail the entire test run due to cleanup issues
  }
}

export default globalTeardown;
