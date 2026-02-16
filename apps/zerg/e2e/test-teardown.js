/**
 * Global test teardown - runs once after all tests complete
 * Modern testing practices 2025: Automatic cleanup of test artifacts
 */

import path from 'path';
import fs from 'fs';
import os from 'os';
import net from 'net';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

async function globalTeardown(config) {
  // Clean up E2E SQLite databases
  const e2eDbDir = process.env.E2E_DB_DIR || path.join(os.tmpdir(), 'zerg_e2e_dbs');

  try {
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

    if (backendRunning) {
      console.log(`E2E teardown: Backend running on ${backendPort}; skipped DB cleanup.`);
      return;
    }

    if (fs.existsSync(e2eDbDir)) {
      fs.rmSync(e2eDbDir, { recursive: true, force: true });
      console.log(`E2E teardown: Cleaned up ${e2eDbDir}`);
    }
  } catch (error) {
    // Best-effort cleanup - globalSetup will handle stale files
    console.error('Test cleanup warning:', error.message);
  }
}

export default globalTeardown;
