import { request } from "./base";

export interface Insight {
  id: string;
  insight_type: string;
  title: string;
  description: string | null;
  project: string | null;
  origin: string | null;
  severity: string;
  confidence: number | null;
  tags: string[] | null;
  observations: string[] | null;
  session_id: string | null;
  archived_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface InsightListResponse {
  insights: Insight[];
  total: number;
}

export async function fetchInsights(filters?: {
  project?: string;
  insight_type?: string;
  since_hours?: number;
  limit?: number;
  include_system?: boolean;
  include_archived?: boolean;
}): Promise<InsightListResponse> {
  const params = new URLSearchParams();
  if (filters?.project) params.set("project", filters.project);
  if (filters?.insight_type) params.set("insight_type", filters.insight_type);
  if (filters?.since_hours)
    params.set("since_hours", String(filters.since_hours));
  if (filters?.limit) params.set("limit", String(filters.limit));
  if (filters?.include_system) params.set("include_system", "true");
  if (filters?.include_archived) params.set("include_archived", "true");
  const qs = params.toString();
  return request<InsightListResponse>(`/insights${qs ? `?${qs}` : ""}`);
}

export async function archiveInsight(id: string): Promise<Insight> {
  return request<Insight>(`/insights/${id}/archive`, {
    method: "POST",
  });
}

export async function unarchiveInsight(id: string): Promise<Insight> {
  return request<Insight>(`/insights/${id}/unarchive`, {
    method: "POST",
  });
}
