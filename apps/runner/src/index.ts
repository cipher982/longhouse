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
import { RunnerWebSocketClient } from './ws-client';

const VERSION = '0.1.2';

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
  console.log(`Usage: longhouse-runner [command] [options]
Commands:
  run                        Start the runner daemon (default)
  doctor                     Diagnose local runner install health
Options:
  -v, --version              Print version and exit
  --envfile <path>           Load env vars from file
  --allow-insecure-envfile   Skip envfile permission check (not recommended)
  --json                     Print JSON for doctor output
  -h, --help                 Show this help`);
  process.exit(0);
}

if (values.version) {
  console.log(`longhouse-runner ${VERSION}`);
  process.exit(0);
}

async function runDaemon() {
  if (values.envfile) {
    try {
      loadEnvfile(values.envfile, { allowInsecure: values['allow-insecure-envfile'] });
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
    clients.forEach((client) => client.stop());
    process.exit(0);
  };

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);

  try {
    await Promise.all(clients.map((client) => client.start()));
  } catch (error) {
    console.error('[main] Failed to start runner:', error);
    process.exit(1);
  }
}

async function main() {
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

  await runDaemon();
}

main().catch((error) => {
  console.error('[main] Unhandled error:', error);
  process.exit(1);
});
