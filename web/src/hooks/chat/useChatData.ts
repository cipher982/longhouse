import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Automation,
  fetchAutomation,
  fetchThreadMessages,
  fetchThreads,
  Thread,
  ThreadMessage,
} from "../../services/api";

interface UseChatDataParams {
  automationId: number | null;
  effectiveThreadId: number | null;
}

export function useChatData({ automationId, effectiveThreadId }: UseChatDataParams) {
  const automationQuery = useQuery<Automation>({
    queryKey: ["automation", automationId],
    queryFn: () => {
      if (automationId == null) {
        return Promise.reject(new Error("Missing automation id"));
      }
      return fetchAutomation(automationId);
    },
    enabled: automationId != null,
  });

  // Fetch chat threads only
  const chatThreadsQuery = useQuery<Thread[]>({
    queryKey: ["threads", automationId, "chat"],
    queryFn: () => {
      if (automationId == null) {
        return Promise.reject(new Error("Missing automation id"));
      }
      return fetchThreads(automationId, "chat");
    },
    enabled: automationId != null,
  });

  // Fetch automation threads (scheduled and manual)
  const automationThreadsQuery = useQuery<Thread[]>({
    queryKey: ["threads", automationId, "automation"],
    queryFn: () => {
      if (automationId == null) {
        return Promise.reject(new Error("Missing automation id"));
      }
      // Fetch both scheduled and manual threads
      return Promise.all([
        fetchThreads(automationId, "scheduled"),
        fetchThreads(automationId, "manual"),
      ]).then(([scheduled, manual]) => [...scheduled, ...manual]);
    },
    enabled: automationId != null,
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

  const isLoading = automationQuery.isLoading || chatThreadsQuery.isLoading || messagesQuery.isLoading;
  const hasError = automationQuery.isError || chatThreadsQuery.isError || messagesQuery.isError;

  return {
    // Queries
    automationQuery,
    chatThreadsQuery,
    automationThreadsQuery,
    messagesQuery,

    // Derived data
    automation: automationQuery.data,
    chatThreads,
    automationThreads,
    messages: messagesQuery.data ?? [],

    // State
    isLoading,
    hasError,
  };
}
