/**
 * API functions for action proposal management.
 *
 * Action proposals are generated during reflection when insights have
 * concrete, actionable fixes. Users review and approve/decline them.
 */

import { request } from "./base";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ActionProposal {
  id: string;
  insight_id: string;
  reflection_run_id: string | null;
  project: string | null;
  title: string;
  action_blurb: string;
  status: "pending" | "approved" | "declined";
  decided_at: string | null;
  task_description: string | null;
  created_at: string;
  insight_type: string | null;
  severity: string | null;
}

export interface ProposalListResponse {
  proposals: ActionProposal[];
  total: number;
}

export interface ProposalActionResponse {
  proposal: ActionProposal;
  task_created: boolean;
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

export async function fetchProposals(filters?: {
  status?: string;
  project?: string;
  limit?: number;
}): Promise<ProposalListResponse> {
  const params = new URLSearchParams();
  if (filters?.status) params.set("status", filters.status);
  if (filters?.project) params.set("project", filters.project);
  if (filters?.limit) params.set("limit", String(filters.limit));
  const qs = params.toString();
  return request<ProposalListResponse>(`/proposals${qs ? `?${qs}` : ""}`);
}

export async function approveProposal(id: string): Promise<ProposalActionResponse> {
  return request<ProposalActionResponse>(`/proposals/${id}/approve`, {
    method: "POST",
  });
}

export async function declineProposal(id: string): Promise<ProposalActionResponse> {
  return request<ProposalActionResponse>(`/proposals/${id}/decline`, {
    method: "POST",
  });
}
