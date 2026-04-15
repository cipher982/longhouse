/**
 * Longhouse Runner Daemon
 *
 * Connects to the Longhouse platform and executes commands on behalf of workers.
 * Enables secure execution without backend access to user SSH keys.
 *
 * Multi-home support: Can connect to multiple backends simultaneously via LONGHOUSE_URLS.
 */

import { parseArgs } from 'util';
import { loadConfig, type RunnerConfig } from './config';
import { loadEnvfile } from './envfile';
import { runDoctorCommand } from './doctor';
import { getRunnerMetadata } from './protocol';
import {
  applyRunnerUpdate,
  checkForRunnerUpdate,
  resolveAutoUpdatePolicy,
  resolveUpdateCheckIntervalSec,
  resolveUpdateJitterSec,
  runUpdateCommand,
} from './update';
import { RUNNER_VERSION } from './version';
import { RunnerWebSocketClient } from './ws-client';
const VERSION = RUNNER_VERSION;

interface RunnerCliValues {
  envfile?: string;
  'allow-insecure-envfile'?: boolean;
}

function printHelp(): void {
  console.log(`Usage: longhouse-runner [command] [options]
Commands:
  run                        Start the runner daemon (default)
  doctor                     Diagnose local runner install health
  update                     Check, apply, or roll back runner updates
Options:
  -v, --version              Print version and exit
  --envfile <path>           Load env vars from file
  --allow-insecure-envfile   Skip envfile permission check (not recommended)
  --json                     Print JSON for doctor output
  -h, --help                 Show this help`);
}

function startAutoUpdateLoop(clients: RunnerWebSocketClient[]): () => void {
  const policy = resolveAutoUpdatePolicy();
  if (policy === 'off') {
    return () => {};
  }

  const intervalMs = resolveUpdateCheckIntervalSec() * 1000;
  const jitterMs = resolveUpdateJitterSec() * 1000;
  let stopped = false;
  let timer: Timer | null = null;

  const scheduleNext = (delayMs: number) => {
    if (stopped) {
      return;
    }

    timer = setTimeout(async () => {
      timer = null;

      if (clients.some((client) => client.getRunningJobCount() > 0)) {
        console.log('[update] Deferring auto-update check while runner jobs are active.');
        scheduleNext(intervalMs);
        return;
      }

      try {
        const result = await checkForRunnerUpdate();
        if (result.update_available && result.blocked_reason) {
          console.log(`[update] Runner update is available but blocked: ${result.blocked_reason}`);
        } else if (result.update_available) {
          console.log(`[update] Runner update available: v${result.current_version} -> v${result.latest_version}`);
          if (policy === 'apply') {
            const applied = await applyRunnerUpdate();
            console.log(`[update] Applied runner update to v${applied.to_version}; exiting so the service manager can relaunch the new binary.`);
            clients.forEach((client) => client.stop());
            process.exit(75);
          }
        }
      } catch (error) {
        console.error('[update] Auto-update check failed:', error);
      } finally {
        const nextDelay = intervalMs + Math.floor(Math.random() * Math.max(1, jitterMs));
        scheduleNext(nextDelay);
      }
    }, delayMs);
  };

  console.log(
    `[update] Auto-update policy ${policy}; checking every ${resolveUpdateCheckIntervalSec()}s with up to ${resolveUpdateJitterSec()}s jitter.`,
  );
  scheduleNext(Math.floor(Math.random() * Math.max(1, jitterMs)));

  return () => {
    stopped = true;
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  };
}

async function runDaemon(values: RunnerCliValues): Promise<void> {
  if (values.envfile) {
    try {
      loadEnvfile(values.envfile, { allowInsecure: values['allow-insecure-envfile'] === true });
    } catch (err) {
      console.error(`Error loading envfile ${values.envfile}:`, err);
      process.exit(1);
    }
  }

  console.log('====================================');
  console.log(`Longhouse Runner v${VERSION}`);
  console.log('====================================');

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

  if (config.longhouseUrls.length === 1) {
    console.log(`Longhouse URL: ${config.longhouseUrls[0]}`);
  } else {
    console.log(`Longhouse URLs (${config.longhouseUrls.length}):`);
    config.longhouseUrls.forEach((url, i) => console.log(`  [${i + 1}] ${url}`));
  }
  console.log('====================================\n');

  const clients = config.longhouseUrls.map((url) => {
    const clientConfig = { ...config, longhouseUrl: url };
    return new RunnerWebSocketClient(clientConfig, getRunnerMetadata);
  });

  const shutdown = () => {
    console.log('\n[main] Shutting down all connections...');
    stopAutoUpdateLoop();
    clients.forEach((client) => client.stop());
    process.exit(0);
  };

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);

  const stopAutoUpdateLoop = startAutoUpdateLoop(clients);

  try {
    await Promise.all(clients.map((client) => client.start()));
  } catch (error) {
    console.error('[main] Failed to start runner:', error);
    process.exit(1);
  }
}

async function main() {
  const rawArgv = process.argv.slice(2);
  if (rawArgv[0] === 'update') {
    const exitCode = await runUpdateCommand(rawArgv.slice(1));
    process.exit(exitCode);
  }

  const { values, positionals } = parseArgs({
    options: {
      version: { type: 'boolean', short: 'v' },
      envfile: { type: 'string' },
      help: { type: 'boolean', short: 'h' },
      'allow-insecure-envfile': { type: 'boolean' },
      json: { type: 'boolean' },
    },
    allowPositionals: true,
  });

  const command = positionals[0] ?? 'run';

  if (values.help) {
    printHelp();
    process.exit(0);
  }

  if (values.version) {
    console.log(`longhouse-runner ${VERSION}`);
    process.exit(0);
  }

  if (command === 'doctor') {
    const exitCode = await runDoctorCommand({
      envfile: values.envfile,
      allowInsecureEnvfile: values['allow-insecure-envfile'],
      json: values.json,
    });
    process.exit(exitCode);
  }

  if (command !== 'run') {
    console.error(`Unknown command: ${command}`);
    console.error('Run with --help for usage.');
    process.exit(1);
  }

  await runDaemon({
    envfile: values.envfile,
    'allow-insecure-envfile': values['allow-insecure-envfile'],
  });
}

main().catch((error) => {
  console.error('[main] Unhandled error:', error);
  process.exit(1);
});
