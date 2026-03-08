import { describe, expect, it } from 'vitest';

import { buildRunnerNativeInstallCommand } from '../runnerInstallCommands';

describe('buildRunnerNativeInstallCommand', () => {
  it('includes runner name when generating a repair command', () => {
    const command = buildRunnerNativeInstallCommand({
      enrollToken: 'token_123',
      longhouseUrl: 'https://david010.longhouse.ai',
      runnerName: 'clifford',
    }, 'server');

    expect(command).toContain('ENROLL_TOKEN=token_123');
    expect(command).toContain('RUNNER_NAME=clifford');
    expect(command).toContain('RUNNER_INSTALL_MODE=server');
  });

  it('uses the one-liner installer for desktop when available', () => {
    const command = buildRunnerNativeInstallCommand({
      enrollToken: 'token_123',
      longhouseUrl: 'https://david010.longhouse.ai',
      oneLinerInstallCommand: 'curl -fsSL example | bash',
      runnerName: 'cinder',
    }, 'desktop');

    expect(command).toBe('curl -fsSL example | bash');
  });
});
