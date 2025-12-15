/**
 * Configuration for the runner daemon.
 *
 * Loads settings from environment variables with sensible defaults.
 */

export interface RunnerConfig {
  swarmletUrl: string;
  runnerId: number;
  runnerSecret: string;
  heartbeatIntervalMs: number;
  reconnectDelayMs: number;
  maxReconnectDelayMs: number;
}

export function loadConfig(): RunnerConfig {
  const swarmletUrl = process.env.SWARMLET_URL || 'ws://localhost:47300';
  const runnerId = parseInt(process.env.RUNNER_ID || '0', 10);
  const runnerSecret = process.env.RUNNER_SECRET || '';

  if (runnerId === 0) {
    throw new Error('RUNNER_ID environment variable is required');
  }

  if (!runnerSecret) {
    throw new Error('RUNNER_SECRET environment variable is required');
  }

  return {
    swarmletUrl,
    runnerId,
    runnerSecret,
    heartbeatIntervalMs: parseInt(process.env.HEARTBEAT_INTERVAL_MS || '30000', 10),
    reconnectDelayMs: parseInt(process.env.RECONNECT_DELAY_MS || '5000', 10),
    maxReconnectDelayMs: parseInt(process.env.MAX_RECONNECT_DELAY_MS || '60000', 10),
  };
}
