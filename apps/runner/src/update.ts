import { parseArgs } from 'node:util';
import { createHash, createPublicKey, verify } from 'node:crypto';
import { existsSync, realpathSync } from 'node:fs';
import { copyFile, mkdir, readFile, rename, rm, symlink, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { basename, dirname, join } from 'node:path';

import { loadEnvfile } from './envfile';
import { DEFAULT_UPDATE_MANIFEST_URL, RUNNER_RELEASE_PUBLIC_KEY_PEM, RUNNER_VERSION } from './version';

export type RunnerAutoUpdatePolicy = 'off' | 'notify' | 'apply';
export type RunnerPlatformTarget = 'darwin-arm64' | 'darwin-x64' | 'linux-x64' | 'linux-arm64';

export interface RunnerUpdateAsset {
  filename: string;
  url: string;
  sha256: string;
  size_bytes: number;
}

export interface RunnerUpdateManifest {
  schema_version: number;
  runner_version: string;
  published_at: string;
  expires_at: string;
  minimum_current_version?: string | null;
  notes_url?: string | null;
  assets: Partial<Record<RunnerPlatformTarget, RunnerUpdateAsset>>;
}

export interface RunnerUpdateState {
  previous_version?: string | null;
  last_checked_at?: string | null;
  last_check_error?: string | null;
  last_check_version?: string | null;
  last_applied_at?: string | null;
  last_applied_from_version?: string | null;
  last_applied_to_version?: string | null;
  last_rolled_back_at?: string | null;
  last_rollback_from_version?: string | null;
  last_rollback_to_version?: string | null;
}

export interface RunnerInstallLayout {
  installRoot: string;
  versionsDir: string;
  downloadsDir: string;
  stateDir: string;
  currentLink: string;
  launcherPath: string;
  updateStatePath: string;
}

export interface RunnerUpdateCheckResult {
  current_version: string;
  installed_version: string | null;
  latest_version: string;
  manifest_url: string;
  policy: RunnerAutoUpdatePolicy;
  update_available: boolean;
  blocked_reason: string | null;
  manifest_expires_at: string;
  asset: RunnerUpdateAsset;
  previous_version: string | null;
  last_checked_at: string;
  last_check_error: string | null;
}

export interface RunnerUpdateApplyResult {
  from_version: string;
  to_version: string;
  binary_path: string;
  install_root: string;
  previous_version: string | null;
  restart_required: boolean;
}

export interface RunnerUpdateRollbackResult {
  from_version: string;
  to_version: string;
  install_root: string;
  restart_required: boolean;
}

export interface RunnerUpdateRuntime {
  env?: NodeJS.ProcessEnv;
  homeDir?: string;
  platform?: NodeJS.Platform;
  arch?: string;
  publicKeyPem?: string;
  fetchImpl?: typeof fetch;
  now?: () => Date;
}

type FetchLike = typeof fetch;

const DEFAULT_UPDATE_CHECK_INTERVAL_SEC = 4 * 60 * 60;
const DEFAULT_UPDATE_JITTER_SEC = 5 * 60;

function parsePositiveInt(raw: string | undefined, fallback: number): number {
  if (!raw) {
    return fallback;
  }
  const parsed = parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return fallback;
  }
  return parsed;
}

export function normalizeAutoUpdatePolicy(raw: string | undefined | null): RunnerAutoUpdatePolicy {
  if (raw === 'apply') {
    return 'apply';
  }
  if (raw === 'off') {
    return 'off';
  }
  return 'notify';
}

export function resolveAutoUpdatePolicy(env: NodeJS.ProcessEnv = process.env): RunnerAutoUpdatePolicy {
  return normalizeAutoUpdatePolicy(env.RUNNER_AUTO_UPDATE_POLICY);
}

export function resolveUpdateManifestUrl(env: NodeJS.ProcessEnv = process.env): string {
  return env.RUNNER_UPDATE_MANIFEST_URL || DEFAULT_UPDATE_MANIFEST_URL;
}

export function resolveUpdateCheckIntervalSec(env: NodeJS.ProcessEnv = process.env): number {
  return parsePositiveInt(env.RUNNER_UPDATE_CHECK_INTERVAL_SEC, DEFAULT_UPDATE_CHECK_INTERVAL_SEC);
}

export function resolveUpdateJitterSec(env: NodeJS.ProcessEnv = process.env): number {
  return parsePositiveInt(env.RUNNER_UPDATE_JITTER_SEC, DEFAULT_UPDATE_JITTER_SEC);
}

export function resolveInstallRoot(
  env: NodeJS.ProcessEnv = process.env,
  homeDir: string = homedir(),
): string {
  if (env.RUNNER_INSTALL_ROOT) {
    return env.RUNNER_INSTALL_ROOT;
  }

  const xdgDataHome = env.XDG_DATA_HOME || join(homeDir, '.local', 'share');
  return join(xdgDataHome, 'longhouse-runner');
}

export function resolveLauncherPath(
  env: NodeJS.ProcessEnv = process.env,
  homeDir: string = homedir(),
): string {
  if (env.RUNNER_LAUNCHER_PATH) {
    return env.RUNNER_LAUNCHER_PATH;
  }

  const xdgBinHome = env.XDG_BIN_HOME || join(homeDir, '.local', 'bin');
  return join(xdgBinHome, 'longhouse-runner');
}

export function resolveInstallLayout(
  env: NodeJS.ProcessEnv = process.env,
  homeDir: string = homedir(),
): RunnerInstallLayout {
  const installRoot = resolveInstallRoot(env, homeDir);
  return {
    installRoot,
    versionsDir: join(installRoot, 'versions'),
    downloadsDir: join(installRoot, 'downloads'),
    stateDir: join(installRoot, 'state'),
    currentLink: join(installRoot, 'current'),
    launcherPath: resolveLauncherPath(env, homeDir),
    updateStatePath: join(installRoot, 'state', 'update-state.json'),
  };
}

export function launcherScriptFor(layout: RunnerInstallLayout): string {
  return `#!/bin/sh
set -eu

CURRENT_LINK=${shellQuote(layout.currentLink)}
TARGET="$CURRENT_LINK/longhouse-runner"

if [ ! -x "$TARGET" ]; then
  echo "Longhouse runner is not installed under $CURRENT_LINK" >&2
  exit 1
fi

exec "$TARGET" "$@"
`;
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\"'\"'`)}'`;
}

function parseSemver(value: string | null | undefined): [number, number, number] | null {
  if (!value) {
    return null;
  }
  const match = value.trim().match(/(\d+)\.(\d+)\.(\d+)/);
  if (!match) {
    return null;
  }
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

export function compareSemver(left: string, right: string): number {
  const leftParts = parseSemver(left);
  const rightParts = parseSemver(right);
  if (!leftParts || !rightParts) {
    return left.localeCompare(right);
  }

  for (let index = 0; index < leftParts.length; index += 1) {
    if (leftParts[index] < rightParts[index]) {
      return -1;
    }
    if (leftParts[index] > rightParts[index]) {
      return 1;
    }
  }
  return 0;
}

function sha256Hex(buffer: Buffer): string {
  return createHash('sha256').update(buffer).digest('hex');
}

export function platformTargetFor(
  platform: NodeJS.Platform = process.platform,
  arch: string = process.arch,
): RunnerPlatformTarget {
  if (platform === 'darwin') {
    if (arch === 'arm64' || arch === 'aarch64') {
      return 'darwin-arm64';
    }
    if (arch === 'x64' || arch === 'x86_64') {
      return 'darwin-x64';
    }
  }
  if (platform === 'linux') {
    if (arch === 'arm64' || arch === 'aarch64') {
      return 'linux-arm64';
    }
    if (arch === 'x64' || arch === 'x86_64') {
      return 'linux-x64';
    }
  }
  throw new Error(`Unsupported platform for runner updates: ${platform}/${arch}`);
}

export function verifyManifestSignature(
  manifestBytes: Buffer,
  signatureBytes: Buffer,
  publicKeyPem: string = RUNNER_RELEASE_PUBLIC_KEY_PEM,
): boolean {
  return verify(
    null,
    manifestBytes,
    createPublicKey(publicKeyPem),
    signatureBytes,
  );
}

export function parseAndValidateManifest(
  manifestBytes: Buffer,
  signatureBytes: Buffer,
  publicKeyPem: string = RUNNER_RELEASE_PUBLIC_KEY_PEM,
  now: Date = new Date(),
): RunnerUpdateManifest {
  if (!verifyManifestSignature(manifestBytes, signatureBytes, publicKeyPem)) {
    throw new Error('Runner update manifest signature is invalid.');
  }

  let parsed: RunnerUpdateManifest;
  try {
    parsed = JSON.parse(manifestBytes.toString('utf-8')) as RunnerUpdateManifest;
  } catch (error) {
    throw new Error(`Runner update manifest is not valid JSON: ${error instanceof Error ? error.message : String(error)}`);
  }

  if (!parsed.runner_version || !parsed.expires_at || !parsed.assets || typeof parsed.assets !== 'object') {
    throw new Error('Runner update manifest is missing required fields.');
  }

  const expiresAt = Date.parse(parsed.expires_at);
  if (!Number.isFinite(expiresAt)) {
    throw new Error('Runner update manifest has an invalid expires_at timestamp.');
  }
  if (expiresAt <= now.getTime()) {
    throw new Error('Runner update manifest has expired.');
  }

  return parsed;
}

export function detectInstalledVersion(layout: RunnerInstallLayout): string | null {
  if (!existsSync(layout.currentLink)) {
    return null;
  }

  try {
    const resolvedCurrent = realpathSync(layout.currentLink);
    return basename(resolvedCurrent);
  } catch {
    return null;
  }
}

function selectAsset(manifest: RunnerUpdateManifest, target: RunnerPlatformTarget): RunnerUpdateAsset {
  const asset = manifest.assets[target];
  if (!asset) {
    throw new Error(`Runner update manifest has no asset for ${target}.`);
  }
  return asset;
}

async function fetchVerifiedManifest(
  manifestUrl: string,
  runtime: RunnerUpdateRuntime = {},
): Promise<RunnerUpdateManifest> {
  const fetchImpl: FetchLike = runtime.fetchImpl ?? fetch;
  const manifestResponse = await fetchImpl(manifestUrl);
  if (!manifestResponse.ok) {
    throw new Error(`Failed to fetch runner update manifest (HTTP ${manifestResponse.status}).`);
  }

  const signatureResponse = await fetchImpl(`${manifestUrl}.sig`);
  if (!signatureResponse.ok) {
    throw new Error(`Failed to fetch runner update signature (HTTP ${signatureResponse.status}).`);
  }

  const manifestBytes = Buffer.from(await manifestResponse.arrayBuffer());
  const signatureBytes = Buffer.from(await signatureResponse.arrayBuffer());
  return parseAndValidateManifest(
    manifestBytes,
    signatureBytes,
    runtime.publicKeyPem ?? RUNNER_RELEASE_PUBLIC_KEY_PEM,
    (runtime.now ?? (() => new Date()))(),
  );
}

function updateBlockedReason(
  currentVersion: string,
  manifest: RunnerUpdateManifest,
): string | null {
  if (compareSemver(currentVersion, manifest.runner_version) >= 0) {
    return null;
  }

  if (manifest.minimum_current_version && compareSemver(currentVersion, manifest.minimum_current_version) < 0) {
    return `Current runner version v${currentVersion} is below the minimum supported update path (${manifest.minimum_current_version}).`;
  }

  return null;
}

async function readUpdateState(layout: RunnerInstallLayout): Promise<RunnerUpdateState> {
  try {
    const raw = await readFile(layout.updateStatePath, 'utf-8');
    return JSON.parse(raw) as RunnerUpdateState;
  } catch {
    return {};
  }
}

async function writeUpdateState(layout: RunnerInstallLayout, state: RunnerUpdateState): Promise<void> {
  await mkdir(layout.stateDir, { recursive: true });
  const tempPath = `${layout.updateStatePath}.tmp-${process.pid}-${Date.now()}`;
  await writeFile(tempPath, `${JSON.stringify(state, null, 2)}\n`, 'utf-8');
  await rename(tempPath, layout.updateStatePath);
}

async function ensureLayout(layout: RunnerInstallLayout): Promise<void> {
  await mkdir(layout.versionsDir, { recursive: true });
  await mkdir(layout.downloadsDir, { recursive: true });
  await mkdir(layout.stateDir, { recursive: true });
  await mkdir(dirname(layout.launcherPath), { recursive: true });
}

async function switchCurrentVersion(layout: RunnerInstallLayout, version: string): Promise<void> {
  const targetPath = join(layout.versionsDir, version);
  if (!existsSync(join(targetPath, 'longhouse-runner'))) {
    throw new Error(`Runner version ${version} is not installed under ${targetPath}.`);
  }

  const tempLink = `${layout.currentLink}.tmp-${process.pid}-${Date.now()}`;
  await symlink(targetPath, tempLink);
  await rename(tempLink, layout.currentLink);
}

async function installLauncher(layout: RunnerInstallLayout): Promise<void> {
  const tempPath = `${layout.launcherPath}.tmp-${process.pid}-${Date.now()}`;
  await writeFile(tempPath, launcherScriptFor(layout), { mode: 0o755 });
  await rename(tempPath, layout.launcherPath);
}

export async function checkForRunnerUpdate(runtime: RunnerUpdateRuntime = {}): Promise<RunnerUpdateCheckResult> {
  const env = runtime.env ?? process.env;
  const now = (runtime.now ?? (() => new Date()))();
  const layout = resolveInstallLayout(env, runtime.homeDir);
  const state = await readUpdateState(layout);
  const currentVersion = detectInstalledVersion(layout) || RUNNER_VERSION;
  const manifestUrl = resolveUpdateManifestUrl(env);
  const manifest = await fetchVerifiedManifest(manifestUrl, runtime);
  const target = platformTargetFor(runtime.platform, runtime.arch);
  const asset = selectAsset(manifest, target);
  const blockedReason = updateBlockedReason(currentVersion, manifest);
  const result: RunnerUpdateCheckResult = {
    current_version: currentVersion,
    installed_version: detectInstalledVersion(layout),
    latest_version: manifest.runner_version,
    manifest_url: manifestUrl,
    policy: resolveAutoUpdatePolicy(env),
    update_available: compareSemver(currentVersion, manifest.runner_version) < 0,
    blocked_reason: blockedReason,
    manifest_expires_at: manifest.expires_at,
    asset,
    previous_version: state.previous_version ?? null,
    last_checked_at: now.toISOString(),
    last_check_error: null,
  };

  await writeUpdateState(layout, {
    ...state,
    last_checked_at: result.last_checked_at,
    last_check_error: null,
    last_check_version: result.latest_version,
  });

  return result;
}

export async function applyRunnerUpdate(
  runtime: RunnerUpdateRuntime = {},
  options: { targetVersion?: string | null } = {},
): Promise<RunnerUpdateApplyResult> {
  const env = runtime.env ?? process.env;
  const now = (runtime.now ?? (() => new Date()))();
  const layout = resolveInstallLayout(env, runtime.homeDir);
  const state = await readUpdateState(layout);
  const currentVersion = detectInstalledVersion(layout) || RUNNER_VERSION;
  const manifestUrl = resolveUpdateManifestUrl(env);
  const manifest = await fetchVerifiedManifest(manifestUrl, runtime);
  const target = platformTargetFor(runtime.platform, runtime.arch);
  const asset = selectAsset(manifest, target);

  if (options.targetVersion && options.targetVersion !== manifest.runner_version) {
    throw new Error(`Manifest latest version is v${manifest.runner_version}, not ${options.targetVersion}.`);
  }

  if (compareSemver(currentVersion, manifest.runner_version) >= 0) {
    throw new Error(`Runner is already on v${currentVersion}; no newer update is available.`);
  }

  const blockedReason = updateBlockedReason(currentVersion, manifest);
  if (blockedReason) {
    throw new Error(blockedReason);
  }

  await ensureLayout(layout);

  const fetchImpl: FetchLike = runtime.fetchImpl ?? fetch;
  const assetResponse = await fetchImpl(asset.url);
  if (!assetResponse.ok) {
    throw new Error(`Failed to download runner update asset (HTTP ${assetResponse.status}).`);
  }

  const binaryBytes = Buffer.from(await assetResponse.arrayBuffer());
  if (binaryBytes.length !== asset.size_bytes) {
    throw new Error(`Runner update size mismatch: expected ${asset.size_bytes}, got ${binaryBytes.length}.`);
  }

  const actualSha = sha256Hex(binaryBytes);
  if (actualSha !== asset.sha256) {
    throw new Error(`Runner update checksum mismatch: expected ${asset.sha256}, got ${actualSha}.`);
  }

  const downloadPath = join(layout.downloadsDir, asset.filename);
  await writeFile(downloadPath, binaryBytes, { mode: 0o755 });

  const versionDir = join(layout.versionsDir, manifest.runner_version);
  await mkdir(versionDir, { recursive: true });
  const binaryPath = join(versionDir, 'longhouse-runner');
  await copyFile(downloadPath, binaryPath);
  await rm(downloadPath, { force: true });

  await switchCurrentVersion(layout, manifest.runner_version);
  await installLauncher(layout);

  await writeUpdateState(layout, {
    ...state,
    previous_version: currentVersion,
    last_checked_at: now.toISOString(),
    last_check_error: null,
    last_check_version: manifest.runner_version,
    last_applied_at: now.toISOString(),
    last_applied_from_version: currentVersion,
    last_applied_to_version: manifest.runner_version,
  });

  return {
    from_version: currentVersion,
    to_version: manifest.runner_version,
    binary_path: binaryPath,
    install_root: layout.installRoot,
    previous_version: currentVersion,
    restart_required: true,
  };
}

export async function rollbackRunnerUpdate(runtime: RunnerUpdateRuntime = {}): Promise<RunnerUpdateRollbackResult> {
  const env = runtime.env ?? process.env;
  const now = (runtime.now ?? (() => new Date()))();
  const layout = resolveInstallLayout(env, runtime.homeDir);
  const state = await readUpdateState(layout);
  const currentVersion = detectInstalledVersion(layout);
  const previousVersion = state.previous_version ?? null;

  if (!currentVersion) {
    throw new Error('Could not determine the currently installed runner version.');
  }
  if (!previousVersion) {
    throw new Error('No previous runner version is recorded for rollback.');
  }

  await switchCurrentVersion(layout, previousVersion);
  await installLauncher(layout);
  await writeUpdateState(layout, {
    ...state,
    previous_version: currentVersion,
    last_rolled_back_at: now.toISOString(),
    last_rollback_from_version: currentVersion,
    last_rollback_to_version: previousVersion,
  });

  return {
    from_version: currentVersion,
    to_version: previousVersion,
    install_root: layout.installRoot,
    restart_required: true,
  };
}

function printJson(payload: unknown): void {
  console.log(JSON.stringify(payload, null, 2));
}

function printCheckResult(result: RunnerUpdateCheckResult): void {
  console.log('====================================');
  console.log('Longhouse Runner Update Check');
  console.log('====================================');
  console.log(`Current version: v${result.current_version}`);
  console.log(`Latest version:  v${result.latest_version}`);
  console.log(`Policy:          ${result.policy}`);
  console.log(`Manifest URL:    ${result.manifest_url}`);
  if (result.update_available) {
    console.log('');
    if (result.blocked_reason) {
      console.log(`Update blocked: ${result.blocked_reason}`);
    } else {
      console.log(`Update available for ${result.asset.filename}.`);
      console.log('Run `longhouse-runner update apply` to stage it.');
    }
  } else {
    console.log('');
    console.log('Runner is already up to date.');
  }
}

function printApplyResult(result: RunnerUpdateApplyResult): void {
  console.log('====================================');
  console.log('Longhouse Runner Update Applied');
  console.log('====================================');
  console.log(`From version:    v${result.from_version}`);
  console.log(`To version:      v${result.to_version}`);
  console.log(`Binary path:     ${result.binary_path}`);
  console.log(`Install root:    ${result.install_root}`);
  console.log('');
  console.log('Restart the runner service to launch the new version if it is already running.');
}

function printRollbackResult(result: RunnerUpdateRollbackResult): void {
  console.log('====================================');
  console.log('Longhouse Runner Rollback Ready');
  console.log('====================================');
  console.log(`From version:    v${result.from_version}`);
  console.log(`To version:      v${result.to_version}`);
  console.log(`Install root:    ${result.install_root}`);
  console.log('');
  console.log('Restart the runner service to launch the rolled-back version if it is already running.');
}

function printUpdateHelp(): void {
  console.log(`Usage: longhouse-runner update <command> [options]
Commands:
  check                       Verify update metadata and report whether a new version exists
  apply                       Download, verify, and stage the latest signed runner update
  rollback                    Switch back to the previously recorded runner version
Options:
  --envfile <path>            Load env vars from file before updating
  --allow-insecure-envfile    Skip envfile permission check (not recommended)
  --json                      Print machine-readable JSON
  --target-version <version>  Require the signed manifest version to match before apply
  -h, --help                  Show this help`);
}

export async function runUpdateCommand(argv: string[]): Promise<number> {
  const { values, positionals } = parseArgs({
    args: argv,
    options: {
      envfile: { type: 'string' },
      'allow-insecure-envfile': { type: 'boolean' },
      json: { type: 'boolean' },
      'target-version': { type: 'string' },
      help: { type: 'boolean', short: 'h' },
    },
    allowPositionals: true,
  });

  if (values.help) {
    printUpdateHelp();
    return 0;
  }

  if (values.envfile) {
    try {
      loadEnvfile(values.envfile, { allowInsecure: values['allow-insecure-envfile'] });
    } catch (error) {
      console.error(`Error loading envfile ${values.envfile}:`, error);
      return 1;
    }
  }

  const command = positionals[0] ?? 'check';

  try {
    if (command === 'check') {
      const result = await checkForRunnerUpdate();
      if (values.json) {
        printJson(result);
      } else {
        printCheckResult(result);
      }
      return result.update_available && !result.blocked_reason ? 10 : 0;
    }

    if (command === 'apply') {
      const result = await applyRunnerUpdate({}, { targetVersion: values['target-version'] ?? null });
      if (values.json) {
        printJson(result);
      } else {
        printApplyResult(result);
      }
      return 0;
    }

    if (command === 'rollback') {
      const result = await rollbackRunnerUpdate();
      if (values.json) {
        printJson(result);
      } else {
        printRollbackResult(result);
      }
      return 0;
    }

    console.error(`Unknown update command: ${command}`);
    printUpdateHelp();
    return 1;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (values.json) {
      printJson({ ok: false, error: message });
    } else {
      console.error(`Update failed: ${message}`);
    }
    return 1;
  }
}
