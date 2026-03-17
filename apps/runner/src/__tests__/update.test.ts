import { afterEach, describe, expect, it } from 'bun:test';
import { createHash, generateKeyPairSync, sign } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  applyRunnerUpdate,
  checkForRunnerUpdate,
  compareSemver,
  launcherScriptFor,
  parseAndValidateManifest,
  platformTargetFor,
  resolveInstallLayout,
  rollbackRunnerUpdate,
  type RunnerUpdateManifest,
} from '../update';

function withTempDir(fn: (dir: string) => Promise<void> | void) {
  const dir = mkdtempSync(join(tmpdir(), 'runner-update-'));
  return Promise.resolve()
    .then(() => fn(dir))
    .finally(() => rmSync(dir, { recursive: true, force: true }));
}

function buildSignedManifest(overrides: Partial<RunnerUpdateManifest> = {}) {
  const { publicKey, privateKey } = generateKeyPairSync('ed25519');
  const manifest: RunnerUpdateManifest = {
    schema_version: 1,
    runner_version: '0.1.5',
    published_at: '2026-03-16T12:00:00Z',
    expires_at: '2099-03-16T12:00:00Z',
    notes_url: 'https://github.com/cipher982/longhouse/releases/tag/runner-v0.1.5',
    assets: {
      'linux-x64': {
        filename: 'longhouse-runner-linux-x64',
        url: 'https://example.com/longhouse-runner-linux-x64',
        sha256: '',
        size_bytes: 0,
      },
      'darwin-arm64': {
        filename: 'longhouse-runner-darwin-arm64',
        url: 'https://example.com/longhouse-runner-darwin-arm64',
        sha256: '',
        size_bytes: 0,
      },
    },
    ...overrides,
  };
  const manifestBytes = Buffer.from(JSON.stringify(manifest, null, 2));
  const signature = sign(null, manifestBytes, privateKey);
  return {
    manifest,
    manifestBytes,
    signature,
    publicKeyPem: publicKey.export({ type: 'spki', format: 'pem' }).toString(),
  };
}

function response(body: string | Buffer, url = 'https://example.com/file', status = 200): Response {
  return new Response(body, { status, headers: { 'content-type': 'application/octet-stream' } });
}

describe('compareSemver', () => {
  it('orders versions as expected', () => {
    expect(compareSemver('0.1.3', '0.1.5')).toBe(-1);
    expect(compareSemver('0.1.5', '0.1.5')).toBe(0);
    expect(compareSemver('0.2.0', '0.1.9')).toBe(1);
  });
});

describe('platformTargetFor', () => {
  it('maps common platforms correctly', () => {
    expect(platformTargetFor('linux', 'x64')).toBe('linux-x64');
    expect(platformTargetFor('linux', 'arm64')).toBe('linux-arm64');
    expect(platformTargetFor('darwin', 'arm64')).toBe('darwin-arm64');
  });
});

describe('parseAndValidateManifest', () => {
  it('accepts a valid signed manifest', () => {
    const signed = buildSignedManifest();
    const manifest = parseAndValidateManifest(signed.manifestBytes, signed.signature, signed.publicKeyPem);
    expect(manifest.runner_version).toBe('0.1.5');
  });

  it('rejects an invalid signature', () => {
    const signed = buildSignedManifest();
    const tampered = Buffer.from(JSON.stringify({ ...signed.manifest, runner_version: '9.9.9' }));
    expect(() => parseAndValidateManifest(tampered, signed.signature, signed.publicKeyPem)).toThrow();
  });
});

describe('launcherScriptFor', () => {
  it('execs the versioned binary through the current symlink', () => {
    const script = launcherScriptFor(resolveInstallLayout({
      RUNNER_INSTALL_ROOT: '/tmp/runner-root',
      RUNNER_LAUNCHER_PATH: '/tmp/bin/longhouse-runner',
    }, '/tmp/home'));
    expect(script).toContain("/tmp/runner-root/current");
    expect(script).toContain('exec "$TARGET" "$@"');
  });
});

describe('runner update flows', () => {
  afterEach(() => {
    delete process.env.RUNNER_INSTALL_ROOT;
    delete process.env.RUNNER_LAUNCHER_PATH;
    delete process.env.RUNNER_UPDATE_MANIFEST_URL;
  });

  it('checks, applies, and rolls back a signed update', async () => {
    await withTempDir(async (dir) => {
      const installRoot = join(dir, 'runner-root');
      const launcherPath = join(dir, 'bin', 'longhouse-runner');
      const versionsDir = join(installRoot, 'versions');
      const currentVersionDir = join(versionsDir, '0.1.3');
      mkdirSync(currentVersionDir, { recursive: true });
      writeFileSync(join(currentVersionDir, 'longhouse-runner'), 'old-binary');
      symlinkSync(currentVersionDir, join(installRoot, 'current'));

      process.env.RUNNER_INSTALL_ROOT = installRoot;
      process.env.RUNNER_LAUNCHER_PATH = launcherPath;
      process.env.RUNNER_UPDATE_MANIFEST_URL = 'https://updates.example.com/manifest.json';

      const binaryBytes = Buffer.from('new-runner-binary');
      const signed = buildSignedManifest({
        assets: {
          'linux-x64': {
            filename: 'longhouse-runner-linux-x64',
            url: 'https://updates.example.com/longhouse-runner-linux-x64',
            sha256: createHash('sha256').update(binaryBytes).digest('hex'),
            size_bytes: binaryBytes.length,
          },
        },
      });

      const fetchImpl: typeof fetch = async (url) => {
        const requestUrl = String(url);
        if (requestUrl === 'https://updates.example.com/manifest.json') {
          return response(signed.manifestBytes);
        }
        if (requestUrl === 'https://updates.example.com/manifest.json.sig') {
          return response(signed.signature);
        }
        if (requestUrl === 'https://updates.example.com/longhouse-runner-linux-x64') {
          return response(binaryBytes);
        }
        return response('missing', requestUrl, 404);
      };

      const check = await checkForRunnerUpdate({
        env: process.env,
        homeDir: dir,
        platform: 'linux',
        arch: 'x64',
        publicKeyPem: signed.publicKeyPem,
        fetchImpl,
      });
      expect(check.update_available).toBe(true);
      expect(check.current_version).toBe('0.1.3');
      expect(check.latest_version).toBe('0.1.5');

      const apply = await applyRunnerUpdate({
        env: process.env,
        homeDir: dir,
        platform: 'linux',
        arch: 'x64',
        publicKeyPem: signed.publicKeyPem,
        fetchImpl,
      });
      expect(apply.from_version).toBe('0.1.3');
      expect(apply.to_version).toBe('0.1.5');
      expect(readFileSync(join(versionsDir, '0.1.5', 'longhouse-runner')).toString()).toBe('new-runner-binary');

      const layout = resolveInstallLayout(process.env, dir);
      expect(readFileSync(layout.launcherPath, 'utf-8')).toContain(`${installRoot}/current`);

      const rollback = await rollbackRunnerUpdate({
        env: process.env,
        homeDir: dir,
      });
      expect(rollback.from_version).toBe('0.1.5');
      expect(rollback.to_version).toBe('0.1.3');
    });
  });
});
