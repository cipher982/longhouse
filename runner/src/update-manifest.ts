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
