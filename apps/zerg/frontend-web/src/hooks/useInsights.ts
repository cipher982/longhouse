import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import {
  archiveInsight,
  fetchInsights,
  unarchiveInsight,
  type Insight,
  type InsightListResponse,
} from "../services/api/insights";

export const insightKeys = {
  all: ["insights"] as const,
  list: (filters?: {
    project?: string;
    insight_type?: string;
    since_hours?: number;
    limit?: number;
    include_system?: boolean;
    include_archived?: boolean;
  }) => [...insightKeys.all, "list", filters] as const,
};

export function useInsights(filters?: {
  project?: string;
  insight_type?: string;
  since_hours?: number;
  limit?: number;
  include_system?: boolean;
  include_archived?: boolean;
}) {
  return useQuery<InsightListResponse, Error>({
    queryKey: insightKeys.list(filters),
    queryFn: () => fetchInsights(filters),
  });
}

export function useArchiveInsight() {
  const queryClient = useQueryClient();

  return useMutation<Insight, Error, string>({
    mutationFn: archiveInsight,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: insightKeys.all });
      toast.success("Insight archived");
    },
    onError: (error) => {
      toast.error(error.message || "Failed to archive insight");
    },
  });
}

export function useUnarchiveInsight() {
  const queryClient = useQueryClient();

  return useMutation<Insight, Error, string>({
    mutationFn: unarchiveInsight,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: insightKeys.all });
      toast.success("Insight restored");
    },
    onError: (error) => {
      toast.error(error.message || "Failed to restore insight");
    },
  });
}
