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
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Load dynamic port from .env file  
function getPortsFromEnv() {
    let BACKEND_PORT = 8001;
    let FRONTEND_PORT = 8002;
    
    // Load from .env file
    const envPath = path.resolve(__dirname, '../.env');
    if (fs.existsSync(envPath)) {
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

if (workerId) {
    console.log(`[spawn-backend] Starting isolated backend for worker ${workerId} on port ${port}`);
} else {
    console.log(`[spawn-backend] Starting single backend on port ${port} (per-worker DB isolation via header)`);
}

// Spawn the test backend with E2E configuration
const backend = spawn('uv', ['run', 'python', '-m', 'uvicorn', 'zerg.main:app', `--host=127.0.0.1`, `--port=${port}`, '--log-level=warning'], {
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
