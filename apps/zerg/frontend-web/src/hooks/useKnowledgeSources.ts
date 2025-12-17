import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import type {
  KnowledgeSource,
  KnowledgeSourceCreate,
  KnowledgeSourceUpdate,
  KnowledgeSearchResult,
  GitHubReposResponse,
  GitHubBranchesResponse,
} from "../services/api";
import {
  fetchKnowledgeSources,
  fetchKnowledgeSource,
  createKnowledgeSource,
  updateKnowledgeSource,
  deleteKnowledgeSource,
  syncKnowledgeSource,
  searchKnowledge,
  fetchGitHubRepos,
  fetchGitHubBranches,
} from "../services/api";

// ---------------------------------------------------------------------------
// Knowledge Sources Hooks
// ---------------------------------------------------------------------------

export function useKnowledgeSources() {
  return useQuery<KnowledgeSource[]>({
    queryKey: ["knowledge-sources"],
    queryFn: fetchKnowledgeSources,
  });
}

export function useKnowledgeSource(id: number) {
  return useQuery<KnowledgeSource>({
    queryKey: ["knowledge-sources", id],
    queryFn: () => fetchKnowledgeSource(id),
    enabled: !!id,
  });
}

export function useCreateKnowledgeSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: KnowledgeSourceCreate) => createKnowledgeSource(data),
    onSuccess: () => {
      toast.success("Knowledge source added");
      queryClient.invalidateQueries({ queryKey: ["knowledge-sources"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to add source: ${error.message}`);
    },
  });
}

export function useUpdateKnowledgeSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: number; data: KnowledgeSourceUpdate }) =>
      updateKnowledgeSource(id, data),
    onSuccess: () => {
      toast.success("Knowledge source updated");
      queryClient.invalidateQueries({ queryKey: ["knowledge-sources"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to update source: ${error.message}`);
    },
  });
}

export function useDeleteKnowledgeSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => deleteKnowledgeSource(id),
    onSuccess: () => {
      toast.success("Knowledge source deleted");
      queryClient.invalidateQueries({ queryKey: ["knowledge-sources"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to delete source: ${error.message}`);
    },
  });
}

export function useSyncKnowledgeSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => syncKnowledgeSource(id),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["knowledge-sources"] });
      if (data.sync_status === "failed") {
        toast.error(data.sync_error || "Sync failed");
      } else {
        toast.success("Sync complete");
      }
    },
    onError: (error: Error) => {
      toast.error(`Failed to sync: ${error.message}`);
    },
  });
}

// ---------------------------------------------------------------------------
// GitHub Repos Hooks
// ---------------------------------------------------------------------------

export function useGitHubRepos(page: number = 1, perPage: number = 30, enabled: boolean = true) {
  return useQuery<GitHubReposResponse>({
    queryKey: ["github-repos", page, perPage],
    queryFn: () => fetchGitHubRepos(page, perPage),
    enabled,
  });
}

export function useGitHubBranches(owner: string, repo: string) {
  return useQuery<GitHubBranchesResponse>({
    queryKey: ["github-branches", owner, repo],
    queryFn: () => fetchGitHubBranches(owner, repo),
    enabled: !!(owner && repo),
  });
}

// ---------------------------------------------------------------------------
// Knowledge Search Hook (V1.1)
// ---------------------------------------------------------------------------

export function useKnowledgeSearch(query: string, limit: number = 10) {
  return useQuery<KnowledgeSearchResult[]>({
    queryKey: ["knowledge-search", query, limit],
    queryFn: () => searchKnowledge(query, limit),
    // Only search when query is at least 2 characters
    enabled: query.length >= 2,
    // Don't refetch on window focus for search
    refetchOnWindowFocus: false,
    // Cache search results for 5 minutes
    staleTime: 5 * 60 * 1000,
  });
}
