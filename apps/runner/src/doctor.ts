import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';

import { loadConfig, type RunnerConfig } from './config';
import { findDefaultEnvfile, loadEnvfile } from './envfile';

export type DoctorSeverity = 'healthy' | 'warning' | 'error';
export type DoctorCheckStatus = 'ok' | 'warn' | 'fail';
export type DoctorInstallMode = 'desktop' | 'server' | 'unknown';

export interface DoctorCheck {
  key: string;
  label: string;
  status: DoctorCheckStatus;
  message: string;
}

export interface DoctorReport {
  severity: DoctorSeverity;
  summary: string;
  installMode: DoctorInstallMode;
  configPath: string | null;
  recommendedAction: string;
  recommendedCommand: string | null;
  checks: DoctorCheck[];
}

export interface DoctorOptions {
  envfile?: string;
  allowInsecureEnvfile?: boolean;
  json?: boolean;
}

export interface DoctorDeps {
  platform: NodeJS.Platform;
  env: NodeJS.ProcessEnv;
  homeDir: string;
  uid: number | null;
  exists: (path: string) => boolean;
  loadConfig: (env?: NodeJS.ProcessEnv) => RunnerConfig;
  runCommand: (command: string, args: string[]) => { status: number | null; stdout: string; stderr: string };
  fetchHealth: (url: string) => Promise<boolean>;
  fetchPreflight: (
    url: string,
    payload: { runnerId: number | null; runnerName: string | null; secret: string },
  ) => Promise<DoctorPreflightResult>;
}

export interface DoctorPreflightResult {
  ok: boolean;
  authenticated: boolean;
  reasonCode: string;
  summary: string;
  httpStatus?: number | null;
  runnerId?: number | null;
  runnerName?: string | null;
  status?: string | null;
  statusReason?: string | null;
  statusSummary?: string | null;
}

export function detectInstallMode(args: {
  platform: NodeJS.Platform;
  env: NodeJS.ProcessEnv;
  configPath: string | null;
  exists: (path: string) => boolean;
  homeDir: string;
}): DoctorInstallMode {
  const envMode = args.env.RUNNER_INSTALL_MODE;
  if (envMode === 'desktop' || envMode === 'server') {
    return envMode;
  }

  if (args.platform === 'darwin') {
    return 'desktop';
  }

  if (args.configPath === '/etc/longhouse/runner.env') {
    return 'server';
  }

  if (args.configPath?.endsWith('/.config/longhouse/runner.env')) {
    return 'desktop';
  }

  const userServicePath = join(args.homeDir, '.config/systemd/user/longhouse-runner.service');
  if (args.exists('/etc/systemd/system/longhouse-runner.service')) {
    return 'server';
  }
  if (args.exists(userServicePath)) {
    return 'desktop';
  }

  return 'unknown';
}

export function expectedServicePath(platform: NodeJS.Platform, installMode: DoctorInstallMode, homeDir: string): string | null {
  if (platform === 'darwin') {
    return join(homeDir, 'Library/LaunchAgents/com.longhouse.runner.plist');
  }
  if (platform !== 'linux') {
    return null;
  }
  if (installMode === 'server') {
    return '/etc/systemd/system/longhouse-runner.service';
  }
  if (installMode === 'desktop') {
    return join(homeDir, '.config/systemd/user/longhouse-runner.service');
  }
  return null;
}

function serviceStatusCommand(platform: NodeJS.Platform, installMode: DoctorInstallMode, uid: number | null): [string, string[]] | null {
  if (platform === 'darwin') {
    const domain = uid === null ? 'gui/$(id -u)' : `gui/${uid}`;
    return ['launchctl', ['print', `${domain}/com.longhouse.runner`]];
  }
  if (platform !== 'linux') {
    return null;
  }
  if (installMode === 'server') {
    return ['systemctl', ['is-active', '--quiet', 'longhouse-runner']];
  }
  if (installMode === 'desktop') {
    return ['systemctl', ['--user', 'is-active', '--quiet', 'longhouse-runner']];
  }
  return null;
}

function restartCommand(platform: NodeJS.Platform, installMode: DoctorInstallMode, uid: number | null): string | null {
  if (platform === 'darwin') {
    const domain = uid === null ? 'gui/$(id -u)' : `gui/${uid}`;
    return `launchctl kickstart -k ${domain}/com.longhouse.runner`;
  }
  if (platform !== 'linux') {
    return null;
  }
  if (installMode === 'server') {
    return 'sudo systemctl restart longhouse-runner';
  }
  if (installMode === 'desktop') {
    return 'systemctl --user restart longhouse-runner';
  }
  return null;
}

function trimTrailingSlash(value: string): string {
  return value.replace(/\/$/, '');
}

function defaultFetchHealth(url: string): Promise<boolean> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  return fetch(`${trimTrailingSlash(url)}/api/health`, { signal: controller.signal })
    .then((response) => response.ok)
    .catch(() => false)
    .finally(() => clearTimeout(timeout));
}

async function defaultFetchPreflight(
  url: string,
  payload: { runnerId: number | null; runnerName: string | null; secret: string },
): Promise<DoctorPreflightResult> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch(`${trimTrailingSlash(url)}/api/runners/preflight`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        runner_id: payload.runnerId,
        runner_name: payload.runnerName,
        secret: payload.secret,
      }),
      signal: controller.signal,
    });

    let body: any = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }

    if (response.status === 404) {
      return {
        ok: false,
        authenticated: false,
        reasonCode: 'preflight_unavailable',
        summary: 'This Longhouse instance is reachable but does not support runner credential preflight yet.',
        httpStatus: response.status,
      };
    }

    if (!response.ok) {
      const detail = body?.detail;
      return {
        ok: false,
        authenticated: false,
        reasonCode: detail?.reason_code || 'preflight_failed',
        summary: detail?.summary || `Longhouse rejected the preflight request (HTTP ${response.status}).`,
        httpStatus: response.status,
      };
    }

    return {
      ok: true,
      authenticated: Boolean(body?.authenticated),
      reasonCode: typeof body?.reason_code === 'string' ? body.reason_code : 'unknown',
      summary: typeof body?.summary === 'string' ? body.summary : 'Longhouse returned an invalid preflight response.',
      httpStatus: response.status,
      runnerId: typeof body?.runner_id === 'number' ? body.runner_id : null,
      runnerName: typeof body?.runner_name === 'string' ? body.runner_name : null,
      status: typeof body?.status === 'string' ? body.status : null,
      statusReason: typeof body?.status_reason === 'string' ? body.status_reason : null,
      statusSummary: typeof body?.status_summary === 'string' ? body.status_summary : null,
    };
  } catch {
    return {
      ok: false,
      authenticated: false,
      reasonCode: 'preflight_unreachable',
      summary: 'Could not reach the runner preflight endpoint on this Longhouse instance.',
    };
  } finally {
    clearTimeout(timeout);
  }
}

function createDefaultDeps(): DoctorDeps {
  return {
    platform: process.platform,
    env: process.env,
    homeDir: homedir(),
    uid: typeof process.getuid === 'function' ? process.getuid() : null,
    exists: existsSync,
    loadConfig,
    runCommand: (command, args) => {
      const result = spawnSync(command, args, { encoding: 'utf-8' });
      return {
        status: result.status,
        stdout: result.stdout ?? '',
        stderr: result.stderr ?? '',
      };
    },
    fetchHealth: defaultFetchHealth,
    fetchPreflight: defaultFetchPreflight,
  };
}

function hasRequiredConfig(env: NodeJS.ProcessEnv): boolean {
  return Boolean((env.LONGHOUSE_URL || env.LONGHOUSE_URLS) && (env.RUNNER_NAME || env.RUNNER_ID) && env.RUNNER_SECRET);
}

export async function collectDoctorReport(options: DoctorOptions = {}, deps: DoctorDeps = createDefaultDeps()): Promise<DoctorReport> {
  const checks: DoctorCheck[] = [];
  let configPath = options.envfile ?? findDefaultEnvfile(deps.platform, deps.exists, deps.homeDir);
  let configError: string | null = null;

  if (configPath) {
    try {
      loadEnvfile(configPath, { allowInsecure: options.allowInsecureEnvfile, env: deps.env });
    } catch (error) {
      configError = error instanceof Error ? error.message : String(error);
    }
  }

  const installMode = detectInstallMode({
    platform: deps.platform,
    env: deps.env,
    configPath,
    exists: deps.exists,
    homeDir: deps.homeDir,
  });

  let config: RunnerConfig | null = null;
  if (configError) {
    checks.push({ key: 'config', label: 'Config', status: 'fail', message: configError });
  } else if (!configPath && !hasRequiredConfig(deps.env)) {
    checks.push({ key: 'config', label: 'Config', status: 'fail', message: 'No runner config found. Generate a repair command in Longhouse and re-run the installer.' });
  } else {
    try {
      config = deps.loadConfig(deps.env);
      const loadedFrom = configPath ?? 'process environment';
      checks.push({ key: 'config', label: 'Config', status: 'ok', message: `Loaded runner config from ${loadedFrom}.` });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      checks.push({ key: 'config', label: 'Config', status: 'fail', message });
      configError = message;
    }
  }

  if (installMode === 'unknown') {
    checks.push({ key: 'install_mode', label: 'Install Mode', status: 'warn', message: 'Install mode is unknown. Re-run the installer once to refresh metadata and service config.' });
  } else {
    checks.push({ key: 'install_mode', label: 'Install Mode', status: 'ok', message: `Detected ${installMode} install mode.` });
  }

  const servicePath = expectedServicePath(deps.platform, installMode, deps.homeDir);
  if (!servicePath) {
    checks.push({ key: 'service_definition', label: 'Service Definition', status: 'warn', message: 'Service check is unavailable on this platform.' });
  } else if (!deps.exists(servicePath)) {
    checks.push({ key: 'service_definition', label: 'Service Definition', status: 'fail', message: `Expected service file is missing: ${servicePath}` });
  } else {
    checks.push({ key: 'service_definition', label: 'Service Definition', status: 'ok', message: `Found service definition at ${servicePath}.` });
  }

  const serviceCommand = serviceStatusCommand(deps.platform, installMode, deps.uid);
  if (!serviceCommand) {
    checks.push({ key: 'service_status', label: 'Service Status', status: 'warn', message: 'Service status check is unavailable.' });
  } else {
    const [command, args] = serviceCommand;
    const result = deps.runCommand(command, args);
    if (result.status === 0) {
      checks.push({ key: 'service_status', label: 'Service Status', status: 'ok', message: 'Runner service is active.' });
    } else {
      checks.push({ key: 'service_status', label: 'Service Status', status: 'fail', message: 'Runner service is installed but not active.' });
    }
  }

  if (config) {
    const reachability = await Promise.all(config.longhouseUrls.map(async (url) => ({ url, ok: await deps.fetchHealth(url) })));
    const reachable = reachability.filter((item) => item.ok);
    if (reachable.length === reachability.length) {
      checks.push({ key: 'connectivity', label: 'Connectivity', status: 'ok', message: `Reached ${reachable.length}/${reachability.length} Longhouse endpoint(s).` });
    } else if (reachable.length > 0) {
      checks.push({ key: 'connectivity', label: 'Connectivity', status: 'warn', message: `Reached ${reachable.length}/${reachability.length} Longhouse endpoint(s).` });
    } else {
      checks.push({ key: 'connectivity', label: 'Connectivity', status: 'fail', message: 'Could not reach any configured Longhouse endpoint.' });
    }
  } else {
    checks.push({ key: 'connectivity', label: 'Connectivity', status: 'warn', message: 'Skipped because runner config is incomplete.' });
  }

  let authenticatedPreflight: DoctorPreflightResult | null = null;
  if (config) {
    const preflights = await Promise.all(
      config.longhouseUrls.map((url) =>
        deps.fetchPreflight(url, {
          runnerId: config.runnerId,
          runnerName: config.runnerName,
          secret: config.runnerSecret,
        }),
      ),
    );
    const successfulPreflights = preflights.filter((result) => result.ok);
    authenticatedPreflight = successfulPreflights.find((result) => result.authenticated) ?? null;

    if (authenticatedPreflight) {
      checks.push({
        key: 'credentials',
        label: 'Credentials',
        status: 'ok',
        message: authenticatedPreflight.summary,
      });

      if (authenticatedPreflight.status === 'online') {
        checks.push({
          key: 'longhouse_view',
          label: 'Longhouse View',
          status: 'ok',
          message: authenticatedPreflight.statusSummary || 'Longhouse currently sees this runner as online.',
        });
      } else if (authenticatedPreflight.statusSummary) {
        checks.push({
          key: 'longhouse_view',
          label: 'Longhouse View',
          status: 'fail',
          message: authenticatedPreflight.statusSummary,
        });
      }
    } else if (successfulPreflights.length > 0) {
      const revoked = successfulPreflights.find((result) => result.reasonCode === 'runner_revoked');
      const invalidSecret = successfulPreflights.find((result) => result.reasonCode === 'invalid_secret');
      const notFound = successfulPreflights.find((result) => result.reasonCode === 'runner_not_found');
      const chosen = revoked || invalidSecret || notFound || successfulPreflights[0];
      checks.push({
        key: 'credentials',
        label: 'Credentials',
        status: 'fail',
        message: chosen.summary,
      });
    } else {
      const unsupported = preflights.find((result) => result.reasonCode === 'preflight_unavailable');
      if (unsupported) {
        checks.push({
          key: 'credentials',
          label: 'Credentials',
          status: 'warn',
          message: unsupported.summary,
        });
      } else {
        checks.push({
          key: 'credentials',
          label: 'Credentials',
          status: 'warn',
          message: 'Skipped because the Longhouse preflight endpoint could not be reached.',
        });
      }
    }
  } else {
    checks.push({ key: 'credentials', label: 'Credentials', status: 'warn', message: 'Skipped because runner config is incomplete.' });
  }

  const hasFail = checks.some((check) => check.status === 'fail');
  const hasWarn = checks.some((check) => check.status === 'warn');
  const recommendedRestart = restartCommand(deps.platform, installMode, deps.uid);

  if (!hasFail && !hasWarn) {
    return {
      severity: 'healthy',
      summary: 'Runner looks healthy on this machine.',
      installMode,
      configPath,
      recommendedAction: 'No action needed.',
      recommendedCommand: null,
      checks,
    };
  }

  const serviceInactive = checks.some((check) => check.key === 'service_status' && check.status === 'fail');
  const serviceMissing = checks.some((check) => check.key === 'service_definition' && check.status === 'fail');
  const connectivityFail = checks.some((check) => check.key === 'connectivity' && check.status === 'fail');
  const credentialsFail = checks.find((check) => check.key === 'credentials' && check.status === 'fail');
  const longhouseViewFail = checks.find((check) => check.key === 'longhouse_view' && check.status === 'fail');

  if (serviceInactive && recommendedRestart) {
    return {
      severity: 'error',
      summary: 'Runner service is installed but not running.',
      installMode,
      configPath,
      recommendedAction: 'Restart the runner service. If it still fails, generate a repair command in Longhouse and re-run the installer.',
      recommendedCommand: recommendedRestart,
      checks,
    };
  }

  if (serviceMissing) {
    return {
      severity: 'error',
      summary: 'Runner service is missing on this machine.',
      installMode,
      configPath,
      recommendedAction: 'Generate a repair command in Longhouse and re-run the installer.',
      recommendedCommand: null,
      checks,
    };
  }

  if (connectivityFail) {
    return {
      severity: 'error',
      summary: 'Runner config looks present, but Longhouse is unreachable from this machine.',
      installMode,
      configPath,
      recommendedAction: 'Check network access and LONGHOUSE_URL. If the URL is correct, try again later.',
      recommendedCommand: null,
      checks,
    };
  }

  if (credentialsFail) {
    const summary = credentialsFail.message.includes('does not know')
      ? 'This Longhouse instance does not know this runner.'
      : credentialsFail.message;
    const recommendedAction = credentialsFail.message.includes('secret')
      ? 'Generate a fresh repair command in Longhouse or rotate the runner secret, then re-run the installer.'
      : credentialsFail.message.includes('revoked')
        ? 'Create a new runner in Longhouse, then run the new repair command on this machine.'
        : 'Check LONGHOUSE_URL and runner identity, then generate a repair command and re-run the installer.';
    return {
      severity: 'error',
      summary,
      installMode,
      configPath,
      recommendedAction,
      recommendedCommand: null,
      checks,
    };
  }

  if (longhouseViewFail && authenticatedPreflight) {
    return {
      severity: 'error',
      summary: 'This machine looks configured, but Longhouse still sees the runner as offline.',
      installMode,
      configPath,
      recommendedAction: 'Restart the runner service. If Longhouse still does not show it online, generate a repair command and re-run the installer.',
      recommendedCommand: recommendedRestart,
      checks,
    };
  }

  return {
    severity: hasFail ? 'error' : 'warning',
    summary: 'Runner has warnings that need attention.',
    installMode,
    configPath,
    recommendedAction: 'Review the checks below, then generate a repair command in Longhouse if needed.',
    recommendedCommand: null,
    checks,
  };
}

function iconFor(status: DoctorCheckStatus): string {
  if (status === 'ok') return '✓';
  if (status === 'warn') return '!';
  return '✗';
}

export function printDoctorReport(report: DoctorReport): void {
  console.log('====================================');
  console.log('Longhouse Runner Doctor');
  console.log('====================================');
  console.log(`Summary: ${report.summary}`);
  console.log(`Install mode: ${report.installMode}`);
  if (report.configPath) {
    console.log(`Config path: ${report.configPath}`);
  }
  console.log('');
  console.log('Checks:');
  for (const check of report.checks) {
    console.log(`  ${iconFor(check.status)} ${check.label}: ${check.message}`);
  }
  console.log('');
  console.log(`Next step: ${report.recommendedAction}`);
  if (report.recommendedCommand) {
    console.log(`Command: ${report.recommendedCommand}`);
  }
}

export async function runDoctorCommand(options: DoctorOptions = {}): Promise<number> {
  const report = await collectDoctorReport(options);
  if (options.json) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    printDoctorReport(report);
  }
  return report.severity === 'error' ? 1 : 0;
}
