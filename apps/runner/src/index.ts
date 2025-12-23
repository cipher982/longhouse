/**
 * Swarmlet Runner Daemon
 *
 * Connects to the Swarmlet platform and executes commands on behalf of workers.
 * Enables secure execution without backend access to user SSH keys.
 */

import { loadConfig } from './config';
import { RunnerWebSocketClient } from './ws-client';
import { getRunnerMetadata } from './protocol';

async function main() {
  console.log('====================================');
  console.log('Swarmlet Runner v0.1.0');
  console.log('====================================');

  // Load configuration
  let config;
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
  console.log(`Swarmlet URL: ${config.swarmletUrl}`);
  console.log(`Heartbeat interval: ${config.heartbeatIntervalMs}ms`);
  console.log('====================================\n');

  // Create and start WebSocket client
  const client = new RunnerWebSocketClient(config, getRunnerMetadata);

  // Handle graceful shutdown
  const shutdown = () => {
    console.log('\n[main] Shutting down...');
    client.stop();
    process.exit(0);
  };

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);

  // Start the client
  try {
    await client.start();
  } catch (error) {
    console.error('[main] Failed to start runner:', error);
    process.exit(1);
  }
}

main().catch((error) => {
  console.error('[main] Unhandled error:', error);
  process.exit(1);
});
