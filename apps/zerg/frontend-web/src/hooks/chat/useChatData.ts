import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Agent,
  fetchAgent,
  fetchThreadMessages,
  fetchThreads,
  fetchWorkflows,
  Thread,
  ThreadMessage,
  Workflow,
} from "../../services/api";

interface UseChatDataParams {
  agentId: number | null;
  effectiveThreadId: number | null;
}

export function useChatData({ agentId, effectiveThreadId }: UseChatDataParams) {
  const agentQuery = useQuery<Agent>({
    queryKey: ["agent", agentId],
    queryFn: () => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return fetchAgent(agentId);
    },
    enabled: agentId != null,
  });

  // Fetch chat threads only
  const chatThreadsQuery = useQuery<Thread[]>({
    queryKey: ["threads", agentId, "chat"],
    queryFn: () => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return fetchThreads(agentId, "chat");
    },
    enabled: agentId != null,
  });

  // Fetch automation threads (scheduled and manual)
  const automationThreadsQuery = useQuery<Thread[]>({
    queryKey: ["threads", agentId, "automation"],
    queryFn: () => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      // Fetch both scheduled and manual threads
      return Promise.all([
        fetchThreads(agentId, "scheduled"),
        fetchThreads(agentId, "manual"),
      ]).then(([scheduled, manual]) => [...scheduled, ...manual]);
    },
    enabled: agentId != null,
  });

  // Fetch workflows for execution in chat
  const workflowsQuery = useQuery<Workflow[]>({
    queryKey: ["workflows"],
    queryFn: fetchWorkflows,
    staleTime: 60000, // Cache for 1 minute
  });

  const messagesQuery = useQuery<ThreadMessage[]>({
    queryKey: ["thread-messages", effectiveThreadId],
    queryFn: () => {
      if (effectiveThreadId == null) {
        return Promise.resolve<ThreadMessage[]>([]);
      }
      return fetchThreadMessages(effectiveThreadId);
    },
    enabled: effectiveThreadId != null,
  });

  const chatThreads = useMemo(() => {
    const list = chatThreadsQuery.data ?? [];
    // Sort threads by updated_at (newest first), falling back to created_at
    return [...list].sort((a, b) => {
      const aTime = a.updated_at || a.created_at;
      const bTime = b.updated_at || b.created_at;
      return bTime.localeCompare(aTime);
    });
  }, [chatThreadsQuery.data]);

  const automationThreads = useMemo(() => {
    const list = automationThreadsQuery.data ?? [];
    // Sort by created_at (newest first)
    return [...list].sort((a, b) => {
      const aTime = a.created_at;
      const bTime = b.created_at;
      return bTime.localeCompare(aTime);
    });
  }, [automationThreadsQuery.data]);

  const isLoading = agentQuery.isLoading || chatThreadsQuery.isLoading || messagesQuery.isLoading;
  const hasError = agentQuery.isError || chatThreadsQuery.isError || messagesQuery.isError;

  return {
    // Queries
    agentQuery,
    chatThreadsQuery,
    automationThreadsQuery,
    workflowsQuery,
    messagesQuery,

    // Derived data
    agent: agentQuery.data,
    chatThreads,
    automationThreads,
    messages: messagesQuery.data ?? [],

    // State
    isLoading,
    hasError,
  };
}
