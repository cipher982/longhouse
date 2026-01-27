import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Fiche,
  fetchFiche,
  fetchThreadMessages,
  fetchThreads,
  fetchWorkflows,
  Thread,
  ThreadMessage,
  Workflow,
} from "../../services/api";

interface UseChatDataParams {
  ficheId: number | null;
  effectiveThreadId: number | null;
}

export function useChatData({ ficheId, effectiveThreadId }: UseChatDataParams) {
  const ficheQuery = useQuery<Fiche>({
    queryKey: ["fiche", ficheId],
    queryFn: () => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      return fetchFiche(ficheId);
    },
    enabled: ficheId != null,
  });

  // Fetch chat threads only
  const chatThreadsQuery = useQuery<Thread[]>({
    queryKey: ["threads", ficheId, "chat"],
    queryFn: () => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      return fetchThreads(ficheId, "chat");
    },
    enabled: ficheId != null,
  });

  // Fetch automation threads (scheduled and manual)
  const automationThreadsQuery = useQuery<Thread[]>({
    queryKey: ["threads", ficheId, "automation"],
    queryFn: () => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      // Fetch both scheduled and manual threads
      return Promise.all([
        fetchThreads(ficheId, "scheduled"),
        fetchThreads(ficheId, "manual"),
      ]).then(([scheduled, manual]) => [...scheduled, ...manual]);
    },
    enabled: ficheId != null,
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

  const isLoading = ficheQuery.isLoading || chatThreadsQuery.isLoading || messagesQuery.isLoading;
  const hasError = ficheQuery.isError || chatThreadsQuery.isError || messagesQuery.isError;

  return {
    // Queries
    ficheQuery,
    chatThreadsQuery,
    automationThreadsQuery,
    workflowsQuery,
    messagesQuery,

    // Derived data
    fiche: ficheQuery.data,
    chatThreads,
    automationThreads,
    messages: messagesQuery.data ?? [],

    // State
    isLoading,
    hasError,
  };
}
