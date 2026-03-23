import { parseArgs } from 'node:util';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

type RunnerPlatformTarget = 'darwin-arm64' | 'darwin-x64' | 'linux-x64' | 'linux-arm64';

type RunnerUpdateAsset = {
  filename: string;
  url: string;
  sha256: string;
  size_bytes: number;
};

type RunnerUpdateManifest = {
  schema_version: number;
  runner_version: string;
  published_at: string;
  expires_at: string;
  notes_url: string;
  assets: Partial<Record<RunnerPlatformTarget, RunnerUpdateAsset>>;
};

const KNOWN_ASSETS: Array<{ filename: string; target: RunnerPlatformTarget }> = [
  { filename: 'longhouse-runner-darwin-arm64', target: 'darwin-arm64' },
  { filename: 'longhouse-runner-darwin-x64', target: 'darwin-x64' },
  { filename: 'longhouse-runner-linux-x64', target: 'linux-x64' },
  { filename: 'longhouse-runner-linux-arm64', target: 'linux-arm64' },
];

function normalizeVersion(input: string): string {
  return input.replace(/^runner-v/, '').replace(/^runner-/, '').replace(/^v/, '');
}

function sha256Hex(buffer: Buffer): string {
  return createHash('sha256').update(buffer).digest('hex');
}

function usage(): void {
  console.log(`Usage: bun apps/runner/scripts/build-update-manifest.ts \\
  --binaries-dir <dir> \\
  --output-dir <dir> \\
  --tag <runner-vX.Y.Z> \\
  --version <vX.Y.Z|X.Y.Z> \\
  --repo <owner/repo>`);
}

const { values } = parseArgs({
  options: {
    'binaries-dir': { type: 'string' },
    'output-dir': { type: 'string' },
    tag: { type: 'string' },
    version: { type: 'string' },
    repo: { type: 'string' },
    help: { type: 'boolean', short: 'h' },
  },
});

if (values.help) {
  usage();
  process.exit(0);
}

if (!values['binaries-dir'] || !values['output-dir'] || !values.tag || !values.version || !values.repo) {
  usage();
  process.exit(1);
}

const binariesDir = resolve(values['binaries-dir']);
const outputDir = resolve(values['output-dir']);
const tag = values.tag;
const version = normalizeVersion(values.version);
const repo = values.repo;

mkdirSync(outputDir, { recursive: true });

const assets: Partial<Record<RunnerPlatformTarget, RunnerUpdateAsset>> = {};
const checksums: string[] = [];

for (const entry of KNOWN_ASSETS) {
  const filePath = join(binariesDir, entry.filename);
  if (!existsSync(filePath)) {
    continue;
  }

  const fileBytes = readFileSync(filePath);
  const sha256 = sha256Hex(fileBytes);
  assets[entry.target] = {
    filename: entry.filename,
    url: `https://github.com/${repo}/releases/download/${tag}/${entry.filename}`,
    sha256,
    size_bytes: fileBytes.length,
  };
  checksums.push(`${sha256}  ${entry.filename}`);
}

if (Object.keys(assets).length === 0) {
  console.error(`No runner binaries found under ${binariesDir}`);
  process.exit(1);
}

const manifest: RunnerUpdateManifest = {
  schema_version: 1,
  runner_version: version,
  published_at: new Date().toISOString(),
  expires_at: new Date(Date.now() + (30 * 24 * 60 * 60 * 1000)).toISOString(),
  notes_url: `https://github.com/${repo}/releases/tag/${tag}`,
  assets,
};

writeFileSync(
  join(outputDir, 'longhouse-runner-manifest.json'),
  `${JSON.stringify(manifest, null, 2)}\n`,
  'utf-8',
);
writeFileSync(join(outputDir, 'checksums.txt'), `${checksums.join('\n')}\n`, 'utf-8');

console.log(`Wrote release metadata to ${outputDir}`);
