#!/usr/bin/env node

/**
 * Spawn isolated test backend for E2E tests
 *
 * This script spawns a dedicated backend server for each Playwright worker,
 * ensuring complete test isolation without shared state.
 */

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

// Load dynamic port from .env file
function getPortsFromEnv() {
    let BACKEND_PORT = 8001;
    let FRONTEND_PORT = 8002;

    // Load from .env file
    const envPath = findDotEnv(__dirname);
    if (envPath && fs.existsSync(envPath)) {
        const envContent = fs.readFileSync(envPath, 'utf8');
        const lines = envContent.split('\n');
        for (const line of lines) {
            const [key, value] = line.split('=');
            if (key === 'BACKEND_PORT') BACKEND_PORT = parseInt(value) || 8001;
            if (key === 'FRONTEND_PORT') FRONTEND_PORT = parseInt(value) || 8002;
        }
    }

    // Allow env vars to override
    BACKEND_PORT = process.env.BACKEND_PORT ? parseInt(process.env.BACKEND_PORT) : BACKEND_PORT;
    FRONTEND_PORT = process.env.FRONTEND_PORT ? parseInt(process.env.FRONTEND_PORT) : FRONTEND_PORT;

    return { BACKEND_PORT, FRONTEND_PORT };
}

// Optional worker ID from command line argument (legacy mode)
const workerId = process.argv[2];
const { BACKEND_PORT } = getPortsFromEnv();

const port = workerId ? BACKEND_PORT + parseInt(workerId) : BACKEND_PORT;

// If specific workerId is set, run single process. Otherwise (global backend mode), scale workers.
  const cpuCount = Math.max(1, os.cpus()?.length ?? 0);
  const envUvicornWorkers = Number.parseInt(process.env.UVICORN_WORKERS ?? "", 10);
  const uvicornWorkers = workerId
    ? 1
    : (Number.isFinite(envUvicornWorkers) && envUvicornWorkers > 0 ? envUvicornWorkers : (process.env.CI ? 4 : cpuCount));

if (workerId) {
    console.log(`[spawn-backend] Starting isolated backend for worker ${workerId} on port ${port}`);
} else {
    console.log(`[spawn-backend] Starting single backend on port ${port} with ${uvicornWorkers} workers (per-worker DB isolation via header)`);
}

// Spawn the test backend with E2E configuration
const backend = spawn('uv', [
    'run', 'python', '-m', 'uvicorn', 'zerg.main:app',
    `--host=127.0.0.1`,
    `--port=${port}`,
    `--workers=${uvicornWorkers}`,
    '--log-level=warning'
], {
    env: {
        ...process.env,
        ENVIRONMENT: 'test:e2e',  // Use E2E test config for real models
        TEST_WORKER_ID: workerId || '0',
        NODE_ENV: 'test',
        TESTING: '1',  // Enable testing mode for database reset
        DEV_ADMIN: process.env.DEV_ADMIN || '1',
        ADMIN_EMAILS: process.env.ADMIN_EMAILS || 'dev@local',
        DATABASE_URL: '',  // Unset DATABASE_URL to force SQLite for E2E tests
        LLM_TOKEN_STREAM: process.env.LLM_TOKEN_STREAM || 'true',  // Enable token streaming for E2E tests
    },
    cwd: join(__dirname, '..', 'backend'),
    stdio: process.env.VERBOSE_BACKEND ? 'inherit' : 'ignore'
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
