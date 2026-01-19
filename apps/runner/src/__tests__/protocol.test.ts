import { describe, expect, it, afterEach } from 'bun:test';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { getRunnerMetadata } from '../protocol';

const originalDockerHost = process.env.DOCKER_HOST;

afterEach(() => {
  if (originalDockerHost === undefined) {
    delete process.env.DOCKER_HOST;
  } else {
    process.env.DOCKER_HOST = originalDockerHost;
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
});
