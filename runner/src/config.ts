/**
 * Configuration for the runner daemon.
 *
 * Loads settings from environment variables with sensible defaults.
 *
 * Authentication: Use either RUNNER_ID or RUNNER_NAME (name is simpler for dev).
 *
 * Multi-home support: Set LONGHOUSE_URLS (comma-separated) to connect to multiple
 * backends simultaneously. Falls back to LONGHOUSE_URL for single-backend mode.
 */

export interface RunnerConfig {
  longhouseUrl: string;
  longhouseUrls: string[];
  runnerId: number | null;
  runnerName: string | null;
  runnerSecret: string;
  heartbeatIntervalMs: number;
  reconnectDelayMs: number;
  maxReconnectDelayMs: number;
  connectTimeoutMs: number;
  capabilities: string[];
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): RunnerConfig {
  // Support both LONGHOUSE_URLS (comma-sep) and LONGHOUSE_URL
  const urlsEnv = env.LONGHOUSE_URLS;
  const urlEnv = env.LONGHOUSE_URL;

  let longhouseUrls: string[];
  if (urlsEnv) {
    // Parse, trim, filter empty, and dedupe URLs
    const parsed = urlsEnv.split(',').map((u) => u.trim()).filter((u) => u);
    longhouseUrls = [...new Set(parsed)];
  } else if (urlEnv) {
    longhouseUrls = [urlEnv];
  } else {
    longhouseUrls = ['ws://localhost:8080'];
  }

  // Validate at least one URL is configured
  if (longhouseUrls.length === 0) {
    throw new Error('LONGHOUSE_URLS is set but contains no valid URLs');
  }

  // longhouseUrl is the first URL
  const longhouseUrl = longhouseUrls[0];
  const runnerId = env.RUNNER_ID ? parseInt(env.RUNNER_ID, 10) : null;
  const runnerName = env.RUNNER_NAME || null;
  const runnerSecret = env.RUNNER_SECRET || '';

  if (!runnerId && !runnerName) {
    throw new Error('Either RUNNER_ID or RUNNER_NAME environment variable is required');
  }

  if (!runnerSecret) {
    throw new Error('RUNNER_SECRET environment variable is required');
  }

  // Parse capabilities from comma-separated list (default: exec.readonly)
  const capabilitiesStr = env.RUNNER_CAPABILITIES || 'exec.readonly';
  const capabilities = capabilitiesStr.split(',').map((s) => s.trim()).filter((s) => s);

  return {
    longhouseUrl,
    longhouseUrls,
    runnerId,
    runnerName,
    runnerSecret,
    heartbeatIntervalMs: parseInt(env.HEARTBEAT_INTERVAL_MS || '30000', 10),
    reconnectDelayMs: parseInt(env.RECONNECT_DELAY_MS || '5000', 10),
    maxReconnectDelayMs: parseInt(env.MAX_RECONNECT_DELAY_MS || '60000', 10),
    connectTimeoutMs: parseInt(env.CONNECT_TIMEOUT_MS || '15000', 10),
    capabilities,
  };
}
