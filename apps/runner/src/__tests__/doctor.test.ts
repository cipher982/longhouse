import { describe, expect, it } from 'bun:test';

import { collectDoctorReport, detectInstallMode, expectedServicePath } from '../doctor';
import type { DoctorDeps } from '../doctor';

function deps(overrides: Partial<DoctorDeps> = {}): DoctorDeps {
  return {
    platform: 'linux',
    env: {
      LONGHOUSE_URL: 'https://david010.longhouse.ai',
      RUNNER_NAME: 'clifford',
      RUNNER_SECRET: 'secret',
      RUNNER_INSTALL_MODE: 'server',
    },
    homeDir: '/home/test',
    uid: 1000,
    exists: (path: string) => path === '/etc/systemd/system/longhouse-runner.service',
    loadConfig: () => ({
      longhouseUrl: 'https://david010.longhouse.ai',
      longhouseUrls: ['https://david010.longhouse.ai'],
      runnerId: null,
      runnerName: 'clifford',
      runnerSecret: 'secret',
      heartbeatIntervalMs: 30000,
      reconnectDelayMs: 5000,
      maxReconnectDelayMs: 60000,
      connectTimeoutMs: 15000,
      capabilities: ['exec.full'],
    }),
    runCommand: () => ({ status: 0, stdout: '', stderr: '' }),
    fetchHealth: async () => true,
    fetchPreflight: async () => ({
      ok: true,
      authenticated: true,
      reasonCode: 'authenticated',
      summary: 'Longhouse accepted the configured runner credentials.',
      status: 'online',
      statusSummary: 'Online. Heartbeats are current.',
    }),
    ...overrides,
  };
}

describe('detectInstallMode', () => {
  it('prefers explicit install mode from env', () => {
    expect(detectInstallMode({
      platform: 'linux',
      env: { RUNNER_INSTALL_MODE: 'desktop' },
      configPath: '/etc/longhouse/runner.env',
      exists: () => false,
      homeDir: '/home/test',
    })).toBe('desktop');
  });

  it('falls back to server for /etc env path', () => {
    expect(detectInstallMode({
      platform: 'linux',
      env: {},
      configPath: '/etc/longhouse/runner.env',
      exists: () => false,
      homeDir: '/home/test',
    })).toBe('server');
  });
});

describe('expectedServicePath', () => {
  it('returns launch agent path on macOS', () => {
    expect(expectedServicePath('darwin', 'desktop', '/Users/test')).toContain('Library/LaunchAgents/com.longhouse.runner.plist');
  });
});

describe('collectDoctorReport', () => {
  it('reports healthy runner when config, service, and connectivity are all good', async () => {
    const report = await collectDoctorReport({}, deps());
    expect(report.severity).toBe('healthy');
    expect(report.recommendedCommand).toBeNull();
  });

  it('recommends restarting inactive service', async () => {
    const report = await collectDoctorReport({}, deps({
      runCommand: () => ({ status: 3, stdout: '', stderr: '' }),
    }));
    expect(report.severity).toBe('error');
    expect(report.summary).toContain('not running');
    expect(report.recommendedCommand).toBe('sudo systemctl restart longhouse-runner');
  });

  it('reports missing config cleanly', async () => {
    const report = await collectDoctorReport({}, deps({
      env: {},
      exists: () => false,
      loadConfig: () => {
        throw new Error('missing config');
      },
    }));
    expect(report.severity).toBe('error');
    expect(report.summary).toContain('warnings');
    expect(report.checks.some((check) => check.key === 'config' && check.status === 'fail')).toBe(true);
  });

  it('flags invalid runner secrets from Longhouse preflight', async () => {
    const report = await collectDoctorReport({}, deps({
      fetchPreflight: async () => ({
        ok: true,
        authenticated: false,
        reasonCode: 'invalid_secret',
        summary: 'Longhouse rejected the configured runner secret.',
      }),
    }));
    expect(report.severity).toBe('error');
    expect(report.summary).toContain('rejected');
  });

  it('flags when Longhouse still sees the runner offline', async () => {
    const report = await collectDoctorReport({}, deps({
      fetchPreflight: async () => ({
        ok: true,
        authenticated: true,
        reasonCode: 'authenticated',
        summary: 'Longhouse accepted the configured runner credentials.',
        status: 'offline',
        statusReason: 'disconnected_recently',
        statusSummary: 'Offline. The runner has no active websocket connection.',
      }),
    }));
    expect(report.severity).toBe('error');
    expect(report.summary).toContain('Longhouse still sees the runner as offline');
  });
});
