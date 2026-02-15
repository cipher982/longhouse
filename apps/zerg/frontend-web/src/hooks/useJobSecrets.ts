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
  type JobSecretListItem,
  type JobSecretUpsertRequest,
  type JobSecretsStatusResponse,
  type JobInfo,
  type JobsRepoStatusResponse,
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
