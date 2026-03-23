import { describe, expect, it, afterEach } from 'bun:test';
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { getRunnerMetadata } from '../protocol';

const originalDockerHost = process.env.DOCKER_HOST;

const originalInstallMode = process.env.RUNNER_INSTALL_MODE;
const originalAutoUpdatePolicy = process.env.RUNNER_AUTO_UPDATE_POLICY;
const originalInstallRoot = process.env.RUNNER_INSTALL_ROOT;
const originalLauncherPath = process.env.RUNNER_LAUNCHER_PATH;

afterEach(() => {
  if (originalDockerHost === undefined) {
    delete process.env.DOCKER_HOST;
  } else {
    process.env.DOCKER_HOST = originalDockerHost;
  }

  if (originalInstallMode === undefined) {
    delete process.env.RUNNER_INSTALL_MODE;
  } else {
    process.env.RUNNER_INSTALL_MODE = originalInstallMode;
  }

  if (originalAutoUpdatePolicy === undefined) {
    delete process.env.RUNNER_AUTO_UPDATE_POLICY;
  } else {
    process.env.RUNNER_AUTO_UPDATE_POLICY = originalAutoUpdatePolicy;
  }

  if (originalInstallRoot === undefined) {
    delete process.env.RUNNER_INSTALL_ROOT;
  } else {
    process.env.RUNNER_INSTALL_ROOT = originalInstallRoot;
  }

  if (originalLauncherPath === undefined) {
    delete process.env.RUNNER_LAUNCHER_PATH;
  } else {
    process.env.RUNNER_LAUNCHER_PATH = originalLauncherPath;
  }
});

function withTempDir(fn: (dir: string) => void) {
  const baseDir = tmpdir();
  mkdirSync(baseDir, { recursive: true });
  const dir = mkdtempSync(join(baseDir, 'runner-docker-'));
  try {
    fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

describe('getRunnerMetadata', () => {
  it('detects docker availability when unix socket exists', () => {
    withTempDir((dir) => {
      const socketPath = join(dir, 'docker.sock');
      writeFileSync(socketPath, '');
      process.env.DOCKER_HOST = `unix://${socketPath}`;

      const metadata = getRunnerMetadata();
      expect(metadata.docker_available).toBe(true);
    });
  });

  it('returns false when unix socket is missing', () => {
    withTempDir((dir) => {
      const socketPath = join(dir, 'missing.sock');
      process.env.DOCKER_HOST = `unix://${socketPath}`;

      const metadata = getRunnerMetadata();
      expect(metadata.docker_available).toBe(false);
    });
  });

  it('includes install mode when configured', () => {
    process.env.RUNNER_INSTALL_MODE = 'server';

    const metadata = getRunnerMetadata();
    expect(metadata.install_mode).toBe('server');
  });

  it('includes the normalized auto-update policy', () => {
    process.env.RUNNER_AUTO_UPDATE_POLICY = 'apply';

    const metadata = getRunnerMetadata();
    expect(metadata.auto_update_policy).toBe('apply');
  });

  it('reports install layout v1 when updater paths are configured', () => {
    withTempDir((dir) => {
      const installRoot = join(dir, 'runner-root');
      const versionsDir = join(installRoot, 'versions');
      const versionDir = join(versionsDir, '0.1.6');
      const launcherPath = join(dir, 'bin', 'longhouse-runner');

      mkdirSync(versionDir, { recursive: true });
      mkdirSync(join(dir, 'bin'), { recursive: true });
      writeFileSync(join(versionDir, 'longhouse-runner'), 'binary');
      writeFileSync(launcherPath, '#!/bin/sh\n');
      symlinkSync(versionDir, join(installRoot, 'current'));

      process.env.RUNNER_INSTALL_ROOT = installRoot;
      process.env.RUNNER_LAUNCHER_PATH = launcherPath;

      const metadata = getRunnerMetadata();
      expect(metadata.install_layout_version).toBe(1);
    });
  });
});
