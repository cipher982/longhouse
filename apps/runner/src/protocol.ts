/**
 * WebSocket protocol message types for runner communication.
 */

import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

// Runner -> Server messages

export interface HelloMessage {
  type: 'hello';
  runner_id?: number | null;
  runner_name?: string | null;
  secret: string;
  metadata: RunnerMetadata;
}

export interface HeartbeatMessage {
  type: 'heartbeat';
}

export interface ExecChunkMessage {
  type: 'exec_chunk';
  job_id: string;
  stream: 'stdout' | 'stderr';
  data: string;
}

export interface ExecDoneMessage {
  type: 'exec_done';
  job_id: string;
  exit_code: number;
  duration_ms: number;
}

export interface ExecErrorMessage {
  type: 'exec_error';
  job_id: string;
  error: string;
}

export type RunnerToServerMessage =
  | HelloMessage
  | HeartbeatMessage
  | ExecChunkMessage
  | ExecDoneMessage
  | ExecErrorMessage;

// Server -> Runner messages

export interface ExecRequestMessage {
  type: 'exec_request';
  job_id: string;
  command: string;
  timeout_secs: number;
}

export interface ExecCancelMessage {
  type: 'exec_cancel';
  job_id: string;
}

export type ServerToRunnerMessage = ExecRequestMessage | ExecCancelMessage;

// Metadata

export interface RunnerMetadata {
  hostname: string;
  platform: string;
  arch: string;
  runner_version: string;
  docker_available?: boolean;
  capabilities?: string[];
}

function detectDockerAvailable(): boolean {
  const dockerHost = process.env.DOCKER_HOST;
  if (dockerHost) {
    if (dockerHost.startsWith('unix://')) {
      const socketPath = dockerHost.slice('unix://'.length);
      if (!socketPath) {
        return false;
      }
      return existsSync(socketPath);
    }
    // For tcp:// or other schemes, assume availability if explicitly configured.
    return true;
  }

  const candidates: string[] = [
    '/var/run/docker.sock',
    '/run/docker.sock',
    join(homedir(), '.docker/run/docker.sock'),
  ];

  if (process.env.XDG_RUNTIME_DIR) {
    candidates.push(join(process.env.XDG_RUNTIME_DIR, 'docker.sock'));
  }

  return candidates.some((socketPath) => existsSync(socketPath));
}

export function getRunnerMetadata(): RunnerMetadata {
  return {
    hostname: process.env.HOSTNAME || 'unknown',
    platform: process.platform,
    arch: process.arch,
    runner_version: '0.1.0',
    docker_available: detectDockerAvailable(),
  };
}
