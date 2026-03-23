import { request } from "./base";
import type {
  KnowledgeSource,
  KnowledgeSourceCreate,
  KnowledgeSourceUpdate,
  KnowledgeSearchResult,
  GitHubReposResponse,
  GitHubBranchesResponse,
} from "./types";

export async function fetchKnowledgeSources(): Promise<KnowledgeSource[]> {
  return request<KnowledgeSource[]>(`/knowledge/sources`);
}

export async function fetchKnowledgeSource(id: number): Promise<KnowledgeSource> {
  return request<KnowledgeSource>(`/knowledge/sources/${id}`);
}

export async function createKnowledgeSource(
  payload: KnowledgeSourceCreate
): Promise<KnowledgeSource> {
  return request<KnowledgeSource>(`/knowledge/sources`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function updateKnowledgeSource(
  id: number,
  payload: KnowledgeSourceUpdate
): Promise<KnowledgeSource> {
  return request<KnowledgeSource>(`/knowledge/sources/${id}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function deleteKnowledgeSource(id: number): Promise<void> {
  return request<void>(`/knowledge/sources/${id}`, {
    method: "DELETE",
  });
}

export async function syncKnowledgeSource(id: number): Promise<KnowledgeSource> {
  return request<KnowledgeSource>(`/knowledge/sources/${id}/sync`, {
    method: "POST",
  });
}

export async function searchKnowledge(query: string, limit: number = 10): Promise<KnowledgeSearchResult[]> {
  return request<KnowledgeSearchResult[]>(`/knowledge/search?q=${encodeURIComponent(query)}&limit=${limit}`);
}

export async function fetchGitHubRepos(
  page: number = 1,
  perPage: number = 30
): Promise<GitHubReposResponse> {
  return request<GitHubReposResponse>(
    `/knowledge/github/repos?page=${page}&per_page=${perPage}`
  );
}

export async function fetchGitHubBranches(
  owner: string,
  repo: string
): Promise<GitHubBranchesResponse> {
  return request<GitHubBranchesResponse>(
    `/knowledge/github/repos/${owner}/${repo}/branches`
  );
}
