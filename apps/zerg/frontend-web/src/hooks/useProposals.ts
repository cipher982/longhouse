/**
 * React Query hooks for action proposal management.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchProposals,
  approveProposal,
  declineProposal,
  type ProposalListResponse,
  type ProposalActionResponse,
} from "../services/api/proposals";
import toast from "react-hot-toast";

// ---------------------------------------------------------------------------
// Query Keys
// ---------------------------------------------------------------------------

export const proposalKeys = {
  all: ["proposals"] as const,
  list: (filters?: { status?: string; project?: string }) =>
    [...proposalKeys.all, "list", filters] as const,
};

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

export function useProposals(filters?: { status?: string; project?: string }) {
  return useQuery<ProposalListResponse, Error>({
    queryKey: proposalKeys.list(filters),
    queryFn: () => fetchProposals(filters),
  });
}

export function useApproveProposal() {
  const queryClient = useQueryClient();

  return useMutation<ProposalActionResponse, Error, string>({
    mutationFn: approveProposal,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: proposalKeys.all });
      toast.success("Proposal approved — task created");
    },
    onError: (error) => {
      toast.error(error.message || "Failed to approve proposal");
    },
  });
}

export function useDeclineProposal() {
  const queryClient = useQueryClient();

  return useMutation<ProposalActionResponse, Error, string>({
    mutationFn: declineProposal,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: proposalKeys.all });
      toast.success("Proposal declined");
    },
    onError: (error) => {
      toast.error(error.message || "Failed to decline proposal");
    },
  });
}
