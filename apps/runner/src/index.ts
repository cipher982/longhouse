/**
 * Longhouse Runner Daemon
 *
 * Connects to the Longhouse platform and executes commands on behalf of workers.
 * Enables secure execution without backend access to user SSH keys.
 *
 * Multi-home support: Can connect to multiple backends simultaneously via LONGHOUSE_URLS.
 */

import { loadConfig, type RunnerConfig } from './config';
import { RunnerWebSocketClient } from './ws-client';
import { getRunnerMetadata } from './protocol';

async function main() {
  console.log('====================================');
  console.log('Longhouse Runner v0.1.0');
  console.log('====================================');

  // Load configuration
  let config: RunnerConfig;
  try {
    config = loadConfig();
  } catch (error) {
    console.error('Configuration error:', error);
    process.exit(1);
  }

  if (config.runnerName) {
    console.log(`Runner Name: ${config.runnerName}`);
  }
  if (config.runnerId) {
    console.log(`Runner ID: ${config.runnerId}`);
  }
  console.log(`Heartbeat interval: ${config.heartbeatIntervalMs}ms`);

  // Log all URLs we're connecting to
  if (config.longhouseUrls.length === 1) {
    console.log(`Longhouse URL: ${config.longhouseUrls[0]}`);
  } else {
    console.log(`Longhouse URLs (${config.longhouseUrls.length}):`);
    config.longhouseUrls.forEach((url, i) => console.log(`  [${i + 1}] ${url}`));
  }
  console.log('====================================\n');

  // Create one client per URL
  const clients = config.longhouseUrls.map((url) => {
    const clientConfig = { ...config, longhouseUrl: url };
    return new RunnerWebSocketClient(clientConfig, getRunnerMetadata);
  });

  // Handle graceful shutdown for all clients
  const shutdown = () => {
    console.log('\n[main] Shutting down all connections...');
    clients.forEach((c) => c.stop());
    process.exit(0);
  };

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);

  // Start all clients in parallel
  try {
    await Promise.all(clients.map((c) => c.start()));
  } catch (error) {
    console.error('[main] Failed to start runner:', error);
    process.exit(1);
  }
}

main().catch((error) => {
  console.error('[main] Unhandled error:', error);
  process.exit(1);
});
