/**
 * Configuration for the runner daemon.
 *
 * Loads settings from environment variables with sensible defaults.
 *
 * Authentication: Use either RUNNER_ID or RUNNER_NAME (name is simpler for dev).
 */

export interface RunnerConfig {
  swarmletUrl: string;
  runnerId: number | null;
  runnerName: string | null;
  runnerSecret: string;
  heartbeatIntervalMs: number;
  reconnectDelayMs: number;
  maxReconnectDelayMs: number;
  capabilities: string[];
}

export function loadConfig(): RunnerConfig {
  const swarmletUrl = process.env.SWARMLET_URL || 'ws://localhost:47300';
  const runnerId = process.env.RUNNER_ID ? parseInt(process.env.RUNNER_ID, 10) : null;
  const runnerName = process.env.RUNNER_NAME || null;
  const runnerSecret = process.env.RUNNER_SECRET || '';

  if (!runnerId && !runnerName) {
    throw new Error('Either RUNNER_ID or RUNNER_NAME environment variable is required');
  }

  if (!runnerSecret) {
    throw new Error('RUNNER_SECRET environment variable is required');
  }

  // Parse capabilities from comma-separated list (default: exec.readonly)
  const capabilitiesStr = process.env.RUNNER_CAPABILITIES || 'exec.readonly';
  const capabilities = capabilitiesStr.split(',').map((s) => s.trim()).filter((s) => s);

  return {
    swarmletUrl,
    runnerId,
    runnerName,
    runnerSecret,
    heartbeatIntervalMs: parseInt(process.env.HEARTBEAT_INTERVAL_MS || '30000', 10),
    reconnectDelayMs: parseInt(process.env.RECONNECT_DELAY_MS || '5000', 10),
    maxReconnectDelayMs: parseInt(process.env.MAX_RECONNECT_DELAY_MS || '60000', 10),
    capabilities,
  };
}
