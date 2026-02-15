import { request } from "./base";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface JobSecretListItem {
  key: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

export interface SecretStatusItem {
  key: string;
  label: string | null;
  type: string;
  placeholder: string | null;
  description: string | null;
  required: boolean;
  configured: boolean;
}

export interface JobSecretsStatusResponse {
  job_id: string;
  secrets: SecretStatusItem[];
}

export interface JobSecretUpsertRequest {
  value: string;
  description?: string;
}

export interface SecretFieldInfo {
  key: string;
  label: string | null;
  type: string;
  placeholder: string | null;
  description: string | null;
  required: boolean;
}

export interface JobInfo {
  id: string;
  cron: string;
  enabled: boolean;
  timeout_seconds: number;
  max_attempts: number;
  tags: string[];
  project: string | null;
  description: string;
  secrets: SecretFieldInfo[];
}

export interface JobListResponse {
  jobs: JobInfo[];
  total: number;
}

export interface JobsRepoStatusResponse {
  initialized: boolean;
  has_remote: boolean;
  remote_url: string | null;
  last_commit_time: string | null;
  last_commit_message: string | null;
  jobs_dir: string;
  job_count: number;
}

export interface EnableJobError {
  detail: string;
  missing?: string[];
}

// ---------------------------------------------------------------------------
// API functions
// ---------------------------------------------------------------------------

export async function listJobSecrets(): Promise<JobSecretListItem[]> {
  return request<JobSecretListItem[]>(`/jobs/secrets`);
}

export async function upsertJobSecret(
  key: string,
  data: JobSecretUpsertRequest,
): Promise<{ success: boolean }> {
  return request<{ success: boolean }>(`/jobs/secrets/${encodeURIComponent(key)}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export async function deleteJobSecret(key: string): Promise<void> {
  await request<void>(`/jobs/secrets/${encodeURIComponent(key)}`, {
    method: "DELETE",
  });
}

export async function getJobSecretsStatus(jobId: string): Promise<JobSecretsStatusResponse> {
  return request<JobSecretsStatusResponse>(`/jobs/${encodeURIComponent(jobId)}/secrets/status`);
}

export async function listJobs(): Promise<JobInfo[]> {
  const response = await request<JobListResponse>(`/jobs/`);
  return response.jobs;
}

export async function getJobsRepoStatus(): Promise<JobsRepoStatusResponse> {
  return request<JobsRepoStatusResponse>(`/jobs/repo`);
}

export async function enableJob(jobId: string, force = false): Promise<JobInfo> {
  const qs = force ? "?force=true" : "";
  return request<JobInfo>(`/jobs/${encodeURIComponent(jobId)}/enable${qs}`, {
    method: "POST",
  });
}

export async function disableJob(jobId: string): Promise<JobInfo> {
  return request<JobInfo>(`/jobs/${encodeURIComponent(jobId)}/disable`, {
    method: "POST",
  });
}
