import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createEnrollToken,
  fetchRunner,
  fetchRunnerDoctor,
  fetchRunnerJobs,
  fetchRunners,
  revokeRunner,
  rotateRunnerSecret,
  updateRunner,
  type EnrollTokenResponse,
  type RotateSecretResponse,
  type Runner,
  type RunnerDoctorResponse,
  type RunnerJob,
  type RunnerUpdate,
} from "../services/api";

// List runners
export function useRunners(options?: { refetchInterval?: number }) {
  return useQuery<Runner[]>({
    queryKey: ["runners"],
    queryFn: fetchRunners,
    refetchInterval: options?.refetchInterval,
  });
}

// Get single runner
export function useRunner(id: number, options?: { refetchInterval?: number }) {
  return useQuery<Runner>({
    queryKey: ["runners", id],
    queryFn: () => fetchRunner(id),
    enabled: id > 0,
    refetchInterval: options?.refetchInterval,
  });
}

export function useRunnerJobs(
  id: number,
  options?: { limit?: number; refetchInterval?: number },
) {
  const limit = options?.limit ?? 6;

  return useQuery<RunnerJob[]>({
    queryKey: ["runners", id, "jobs", limit],
    queryFn: () => fetchRunnerJobs(id, limit),
    enabled: id > 0,
    refetchInterval: options?.refetchInterval,
  });
}

export function useRunnerDoctor() {
  return useMutation<RunnerDoctorResponse, Error, number>({
    mutationFn: (id: number) => fetchRunnerDoctor(id),
  });
}

// Create enroll token
export function useCreateEnrollToken() {
  return useMutation<EnrollTokenResponse>({
    mutationFn: createEnrollToken,
  });
}

// Update runner
export function useUpdateRunner() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: number; data: RunnerUpdate }) =>
      updateRunner(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["runners"] });
    },
  });
}

// Revoke runner
export function useRevokeRunner() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => revokeRunner(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["runners"] });
    },
  });
}

// Rotate runner secret
export function useRotateRunnerSecret() {
  const queryClient = useQueryClient();
  return useMutation<RotateSecretResponse, Error, number>({
    mutationFn: (id: number) => rotateRunnerSecret(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["runners"] });
    },
  });
}
