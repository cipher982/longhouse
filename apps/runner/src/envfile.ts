import { existsSync, readFileSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

export function loadEnvfile(path: string, options?: { allowInsecure?: boolean; env?: NodeJS.ProcessEnv }): void {
  const env = options?.env ?? process.env;
  const stats = statSync(path);
  const mode = stats.mode;
  const insecurePerms = mode & 0o077;
  if (insecurePerms && !options?.allowInsecure) {
    throw new Error(
      `Envfile ${path} has insecure permissions (mode ${(mode & 0o777).toString(8)}). ` +
      `Fix with: chmod 600 ${path}`,
    );
  }

  const content = readFileSync(path, 'utf-8');
  for (const line of content.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eqIndex = trimmed.indexOf('=');
    if (eqIndex <= 0) continue;
    const key = trimmed.slice(0, eqIndex).trim();
    const value = trimmed.slice(eqIndex + 1).trim();
    env[key] = value;
  }
}

export function findDefaultEnvfile(
  platform: NodeJS.Platform = process.platform,
  exists: (path: string) => boolean = existsSync,
  home: string = homedir(),
): string | null {
  const candidates = platform === 'linux'
    ? ['/etc/longhouse/runner.env', join(home, '.config/longhouse/runner.env')]
    : [join(home, '.config/longhouse/runner.env')];

  for (const candidate of candidates) {
    if (exists(candidate)) {
      return candidate;
    }
  }

  return null;
}
