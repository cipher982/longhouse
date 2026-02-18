import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import {
  listJobSecrets,
  upsertJobSecret,
  deleteJobSecret,
  getJobSecretsStatus,
  listJobs,
  getJobsRepoStatus,
  enableJob,
  disableJob,
  getRepoConfig,
  saveRepoConfig,
  verifyRepoConfig,
  deleteRepoConfig,
  getRecentJobRuns,
  getLastJobRuns,
  getJobRuns,
  type JobSecretListItem,
  type JobSecretUpsertRequest,
  type JobSecretsStatusResponse,
  type JobInfo,
  type JobsRepoStatusResponse,
  type JobRepoConfigResponse,
  type JobRepoConfigRequest,
  type JobRepoVerifyResponse,
  type JobRunHistoryResponse,
  type JobLastRunResponse,
} from "../services/api/jobSecrets";

// List all secrets (keys only, no values)
export function useJobSecrets() {
  return useQuery<JobSecretListItem[]>({
    queryKey: ["job-secrets"],
    queryFn: listJobSecrets,
  });
}

// Get secrets status for a specific job
export function useJobSecretsStatus(jobId: string | null) {
  return useQuery<JobSecretsStatusResponse>({
    queryKey: ["job-secrets-status", jobId],
    queryFn: () => getJobSecretsStatus(jobId!),
    enabled: !!jobId,
  });
}

// Upsert a secret
export function useUpsertJobSecret() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ key, data }: { key: string; data: JobSecretUpsertRequest }) =>
      upsertJobSecret(key, data),
    onSuccess: () => {
      toast.success("Secret saved");
      queryClient.invalidateQueries({ queryKey: ["job-secrets"] });
      queryClient.invalidateQueries({ queryKey: ["job-secrets-status"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to save secret: ${error.message}`);
    },
  });
}

// List all registered jobs
export function useJobs() {
  return useQuery<JobInfo[]>({
    queryKey: ["jobs"],
    queryFn: listJobs,
  });
}

// Jobs repo status
export function useJobsRepoStatus() {
  return useQuery<JobsRepoStatusResponse>({
    queryKey: ["jobs-repo-status"],
    queryFn: getJobsRepoStatus,
  });
}

// Enable a job (with optional force bypass)
export function useEnableJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ jobId, force }: { jobId: string; force?: boolean }) =>
      enableJob(jobId, force),
    onSuccess: (_data, { jobId }) => {
      toast.success(`Job ${jobId} enabled`);
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
}

// Disable a job
export function useDisableJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => disableJob(jobId),
    onSuccess: (_data, jobId) => {
      toast.success(`Job ${jobId} disabled`);
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to disable job: ${error.message}`);
    },
  });
}

// Recent runs across all jobs
export function useRecentJobRuns(limit = 10) {
  return useQuery<JobRunHistoryResponse>({
    queryKey: ["job-runs-recent", limit],
    queryFn: () => getRecentJobRuns(limit),
  });
}

// Last run per job (accurate, not capped)
export function useLastJobRuns() {
  return useQuery<JobLastRunResponse>({
    queryKey: ["job-runs-last"],
    queryFn: getLastJobRuns,
  });
}

// Runs for a specific job
export function useJobRuns(jobId: string | null, limit = 25) {
  return useQuery<JobRunHistoryResponse>({
    queryKey: ["job-runs", jobId],
    queryFn: () => getJobRuns(jobId!, limit),
    enabled: !!jobId,
  });
}

// Delete a secret
export function useDeleteJobSecret() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => deleteJobSecret(key),
    onSuccess: () => {
      toast.success("Secret deleted");
      queryClient.invalidateQueries({ queryKey: ["job-secrets"] });
      queryClient.invalidateQueries({ queryKey: ["job-secrets-status"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to delete secret: ${error.message}`);
    },
  });
}

// ---------------------------------------------------------------------------
// Repo Config hooks
// ---------------------------------------------------------------------------

// Get repo config (returns null-ish on 404 = not configured)
export function useRepoConfig() {
  return useQuery<JobRepoConfigResponse | null>({
    queryKey: ["repo-config"],
    queryFn: async () => {
      try {
        return await getRepoConfig();
      } catch (err) {
        // 404 means not configured — return null instead of throwing
        if (err instanceof Error && "status" in err && (err as { status: number }).status === 404) {
          return null;
        }
        throw err;
      }
    },
  });
}

// Save repo config
export function useSaveRepoConfig() {
  const queryClient = useQueryClient();
  return useMutation<{ success: boolean }, Error, JobRepoConfigRequest>({
    mutationFn: (config) => saveRepoConfig(config),
    onSuccess: () => {
      toast.success("Repo connected! Syncing...");
      queryClient.invalidateQueries({ queryKey: ["repo-config"] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["jobs-repo-status"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to save repo config: ${error.message}`);
    },
  });
}

// Verify repo config
export function useVerifyRepoConfig() {
  return useMutation<JobRepoVerifyResponse, Error, JobRepoConfigRequest>({
    mutationFn: (config) => verifyRepoConfig(config),
  });
}

// Delete repo config
export function useDeleteRepoConfig() {
  const queryClient = useQueryClient();
  return useMutation<void, Error>({
    mutationFn: () => deleteRepoConfig(),
    onSuccess: () => {
      toast.success("Repo disconnected");
      queryClient.invalidateQueries({ queryKey: ["repo-config"] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["jobs-repo-status"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to disconnect repo: ${error.message}`);
    },
  });
}
