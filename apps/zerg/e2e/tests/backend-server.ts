/**
 * Backend server management for E2E tests
 *
 * This module provides utilities to start and stop isolated backend servers
 * for each test worker, ensuring complete test isolation.
 */

import { spawn, ChildProcess } from 'child_process';
import { join } from 'path';
import * as fs from 'fs';
import * as path from 'path';

// Load dynamic backend port from .env
function getBackendPort(): number {
  // Check environment variable first
  if (process.env.BACKEND_PORT) {
    return parseInt(process.env.BACKEND_PORT);
  }

  // Load from .env file
  const envPath = path.resolve(__dirname, '../../../.env');
  if (fs.existsSync(envPath)) {
    const envContent = fs.readFileSync(envPath, 'utf8');
    const lines = envContent.split('\n');
    for (const line of lines) {
      const [key, value] = line.split('=');
      if (key === 'BACKEND_PORT') {
        return parseInt(value) || 8001;
      }
    }
  }

  return 8001; // Default fallback
}

interface BackendServer {
  port: number;
  baseUrl: string;
  process?: ChildProcess;
}

const runningServers = new Map<string, BackendServer>();

export async function startBackendServer(workerId: string): Promise<BackendServer> {
  // Single shared backend is started by Playwright webServer; just return baseUrl
  const basePort = getBackendPort();
  const baseUrl = `http://localhost:${basePort}`;

  const server: BackendServer = {
    port: basePort,
    baseUrl,
    process: undefined,
  };

  // Ensure it's reachable before proceeding
  await waitForServer(baseUrl);
  console.log(`[backend-server] Backend ready (shared) for worker ${workerId} at ${baseUrl}`);
  return server;
}

export async function stopBackendServer(_workerId: string): Promise<void> {
  // No-op in shared backend mode
}

export async function stopAllBackendServers(): Promise<void> {
  console.log(`[backend-server] Stopping all backend servers`);
  const promises = Array.from(runningServers.keys()).map(stopBackendServer);
  await Promise.all(promises);
}

async function waitForServer(baseUrl: string, maxAttempts = 30): Promise<void> {
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      // Prefer a known health/info endpoint
      const response = await fetch(`${baseUrl}/api/system/info`, { method: 'GET' });
      if (response.ok || response.status === 200) {
        return; // Server is ready
      }
    } catch (error) {
      // Server not ready yet, continue waiting
    }

    if (attempt < maxAttempts) {
      await new Promise(resolve => setTimeout(resolve, 1000)); // Wait 1 second
    }
  }

  throw new Error(`Backend server at ${baseUrl} did not start within ${maxAttempts} seconds`);
}

// Clean up on process exit
process.on('exit', () => {
  for (const [workerId, server] of runningServers.entries()) {
    if (server.process) {
      console.log(`[backend-server] Cleaning up backend for worker ${workerId}`);
      server.process.kill('SIGKILL');
    }
  }
});

process.on('SIGTERM', async () => {
  await stopAllBackendServers();
  process.exit(0);
});

process.on('SIGINT', async () => {
  await stopAllBackendServers();
  process.exit(0);
});
