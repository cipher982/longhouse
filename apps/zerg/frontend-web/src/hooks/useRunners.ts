import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createEnrollToken,
  fetchRunner,
  fetchRunners,
  revokeRunner,
  updateRunner,
  type EnrollTokenResponse,
  type Runner,
  type RunnerUpdate,
} from "../services/api";

// List runners
export function useRunners() {
  return useQuery<Runner[]>({
    queryKey: ["runners"],
    queryFn: fetchRunners,
  });
}

// Get single runner
export function useRunner(id: number) {
  return useQuery<Runner>({
    queryKey: ["runners", id],
    queryFn: () => fetchRunner(id),
    enabled: id > 0,
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
