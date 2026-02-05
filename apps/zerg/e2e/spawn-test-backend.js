#!/usr/bin/env node

/**
 * Spawn isolated test backend for E2E tests
 *
 * SQLite-based isolation: Each E2E test run uses a dedicated SQLite database
 * in a temp directory. No Postgres required.
 */

import crypto from 'crypto';
import { spawn } from 'child_process';
import { join } from 'path';
import fs from 'fs';
import path from 'path';
import os from 'os';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function findDotEnv(startDir) {
    let dir = startDir;
    for (let i = 0; i < 8; i++) {
        const candidate = path.join(dir, '.env');
        if (fs.existsSync(candidate)) return candidate;
        const parent = path.dirname(dir);
        if (parent === dir) break;
        dir = parent;
    }
    return null;
}

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
        const isQuoted = (value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"));
        if (!isQuoted) {
            // Strip inline comments for common `.env` style: KEY=value # comment
            value = value.replace(/\s+#.*$/, '').trim();
        }
        if (isQuoted) {
            value = value.slice(1, -1);
        }
        if (process.env[key] === undefined) {
            process.env[key] = value;
        }
    }
}

// Ensure local runs inherit repo-root .env (Playwright also loads it, but this helps direct execution).
{
    const envPath = findDotEnv(__dirname);
    if (envPath) loadDotEnv(envPath);
}

if (!process.env.FERNET_SECRET) {
    const raw = crypto.randomBytes(32).toString('base64');
    const urlSafe = raw.replace(/\+/g, '-').replace(/\//g, '_');
    process.env.FERNET_SECRET = urlSafe;
    console.log('[spawn-backend] FERNET_SECRET not set; generated ephemeral key for tests.');
}

// Backend port comes from env (set by playwright.config.js random port generation)
// This script is spawned by Playwright, so env vars are already set
function getBackendPort() {
    const port = parseInt(process.env.BACKEND_PORT || '');
    if (!port || isNaN(port)) {
        throw new Error('BACKEND_PORT env var required (set by playwright.config.js)');
    }
    return port;
}

// Optional worker ID from command line argument (legacy mode)
const workerId = process.argv[2];
const BACKEND_PORT = getBackendPort();

const port = workerId ? BACKEND_PORT + parseInt(workerId) : BACKEND_PORT;

// E2E tests now use SQLite per-backend-instance for isolation
// No need for multiple uvicorn workers since SQLite is single-writer
const uvicornWorkers = 1;

// Create E2E SQLite database path
const e2eDbDir = process.env.E2E_DB_DIR || path.join(os.tmpdir(), 'zerg_e2e_dbs');
if (!fs.existsSync(e2eDbDir)) {
    fs.mkdirSync(e2eDbDir, { recursive: true });
}
const dbPath = path.join(e2eDbDir, `e2e_${port}.db`);
const databaseUrl = `sqlite:///${dbPath}`;
const toolStubsPath = join(__dirname, 'fixtures', 'tool-stubs.json');

console.log(`[spawn-backend] Starting E2E backend on port ${port} with SQLite: ${dbPath}`);

// Spawn the test backend with E2E configuration
const backend = spawn('uv', [
    'run', 'python', '-m', 'uvicorn', 'zerg.main:app',
    `--host=127.0.0.1`,
    `--port=${port}`,
    `--workers=${uvicornWorkers}`,
    '--log-level=error'  // Only show errors, not INFO logs (reduces output from 26K to ~100 lines)
], {
    env: {
        ...process.env,
        // Add e2e/bin to PATH for mock-hatch CLI (used by workspace agents in E2E)
        PATH: `${join(__dirname, 'bin')}:${process.env.PATH || ''}`,
        ENVIRONMENT: 'test:e2e',  // Use E2E test config for real models
        TEST_WORKER_ID: workerId || '0',
        NODE_ENV: 'test',
        TESTING: '1',  // Enable testing mode for database reset
        AUTH_DISABLED: '1',  // Disable auth for E2E tests
        DEV_ADMIN: process.env.DEV_ADMIN || '1',
        ADMIN_EMAILS: process.env.ADMIN_EMAILS || 'dev@local',
        // SQLite database for this E2E backend instance
        DATABASE_URL: databaseUrl,
        LLM_TOKEN_STREAM: process.env.LLM_TOKEN_STREAM || 'true',  // Enable token streaming for E2E tests
        // Force deterministic model for E2E chat flows (UI uses default model id)
        E2E_DEFAULT_MODEL: process.env.E2E_DEFAULT_MODEL || 'gpt-scripted',
        // LIFE_HUB_URL and LIFE_HUB_API_KEY inherited from environment (for session continuity tests)
        // Workspace path for workspace agents (use temp dir in E2E, not /var/oikos)
        OIKOS_WORKSPACE_PATH: process.env.OIKOS_WORKSPACE_PATH || os.tmpdir() + '/zerg-e2e-workspaces',
        // Claude config dir for session files (use temp dir in E2E)
        CLAUDE_CONFIG_DIR: process.env.CLAUDE_CONFIG_DIR || os.tmpdir() + '/zerg-e2e-claude',
        // Mock hatch CLI for workspace agents in E2E (can't run real Claude Code fiches)
        E2E_HATCH_PATH: join(__dirname, 'bin', 'hatch'),
        // Deterministic tool stubs for E2E (runner_exec/ssh_exec/web_search)
        LONGHOUSE_TOOL_STUBS_PATH: toolStubsPath,
        // Suppress Python logging noise for E2E tests
        LOG_LEVEL: 'ERROR',
    },
    cwd: join(__dirname, '..', 'backend'),
    // Inherit stdio so Playwright can detect startup and we can see errors
    stdio: 'inherit'
});

// Handle backend process events
backend.on('error', (error) => {
    console.error(`[spawn-backend] Worker ${workerId} backend error:`, error);
    process.exit(1);
});

backend.on('close', (code) => {
    console.log(`[spawn-backend] Worker ${workerId} backend exited with code ${code}`);
    process.exit(code);
});

// Forward signals to backend process
process.on('SIGTERM', () => {
    console.log(`[spawn-backend] Worker ${workerId} received SIGTERM, shutting down backend`);
    backend.kill('SIGTERM');
});

process.on('SIGINT', () => {
    console.log(`[spawn-backend] Worker ${workerId} received SIGINT, shutting down backend`);
    backend.kill('SIGINT');
});

// Keep the spawner running
process.stdin.resume();
