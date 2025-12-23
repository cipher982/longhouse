/**
 * WebSocket protocol message types for runner communication.
 */

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

export function getRunnerMetadata(): RunnerMetadata {
  return {
    hostname: process.env.HOSTNAME || 'unknown',
    platform: process.platform,
    arch: process.arch,
    runner_version: '0.1.0',
    docker_available: false, // TODO: Detect docker in Phase 3
  };
}
