import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { logger } from "../../lib/logger";
import {
  postThreadMessage,
  startThreadRun,
  Thread,
  ThreadMessage,
  updateThread,
} from "../../services/api";

interface UseChatActionsParams {
  automationId: number | null;
  effectiveThreadId: number | null;
}

export function useChatActions({ automationId, effectiveThreadId }: UseChatActionsParams) {
  const queryClient = useQueryClient();

  const sendMutation = useMutation<
    ThreadMessage,
    Error,
    { threadId: number; content: string },
    number
  >({
    mutationFn: async ({ threadId, content }) => {
      logger.debug(`[Chat] Sending message to thread: ${threadId}`);
      const message = await postThreadMessage(threadId, content);
      logger.debug(`[Chat] Triggering thread run: ${threadId}`);
      await startThreadRun(threadId);
      logger.debug('[Chat] Run started');
      return message;
    },
    onMutate: async ({ threadId, content }) => {
      await queryClient.cancelQueries({ queryKey: ["thread-messages", threadId] });

      const optimisticId = -Date.now();
      // Optimistic message with current time - server will override with its own timestamp
      // Since clocks are usually synced, the displayed time won't change noticeably
      const optimisticMessage = {
        id: optimisticId,
        thread_id: threadId,
        role: "user",
        content,
        sent_at: new Date().toISOString(),
        processed: true,
      } satisfies ThreadMessage;

      queryClient.setQueryData<ThreadMessage[]>(["thread-messages", threadId], (old) =>
        old ? [...old, optimisticMessage] : [optimisticMessage]
      );

      return optimisticId;
    },
    onError: (_error, variables, optimisticId) => {
      queryClient.setQueryData<ThreadMessage[]>(
        ["thread-messages", variables.threadId],
        (current) => current?.filter((msg) => msg.id !== optimisticId) ?? []
      );
      toast.error("Failed to send message", { duration: 6000 });
    },
    onSuccess: (data, variables, optimisticId) => {
      queryClient.setQueryData<ThreadMessage[]>(
        ["thread-messages", variables.threadId],
        (current) =>
          current?.map((msg) => (msg.id === optimisticId ? data : msg)) ?? [data]
      );
    },
    onSettled: (_data, _error, variables) => {
      queryClient.invalidateQueries({ queryKey: ["thread-messages", variables.threadId] });
      // Also refresh threads to sync with server state
      if (automationId != null) {
        queryClient.invalidateQueries({ queryKey: ["threads", automationId, "chat"] });
      }
    },
  });

  const renameThreadMutation = useMutation<
    Thread,
    Error,
    { threadId: number; title: string },
    { previousThreads?: Thread[] }
  >({
    mutationFn: ({ threadId, title }) => updateThread(threadId, { title }),
    onMutate: async ({ threadId, title }) => {
      if (automationId == null) {
        return {};
      }
      const queryKey = ["threads", automationId, "chat"] as const;
      await queryClient.cancelQueries({ queryKey });
      const previousThreads = queryClient.getQueryData<Thread[]>(queryKey);
      queryClient.setQueryData<Thread[]>(queryKey, (old) =>
        old ? old.map((thread) => (thread.id === threadId ? { ...thread, title } : thread)) : old
      );
      return { previousThreads };
    },
    onError: (error, _variables, context) => {
      if (automationId == null) {
        return;
      }
      if (context?.previousThreads) {
        queryClient.setQueryData(["threads", automationId, "chat"], context.previousThreads);
      }
      toast.error("Failed to rename thread", {
        duration: 6000,
      });
    },
    onSuccess: (updatedThread) => {
      if (automationId != null) {
        queryClient.setQueryData<Thread[]>(["threads", automationId, "chat"], (old) =>
          old ? old.map((thread) => (thread.id === updatedThread.id ? updatedThread : thread)) : old
        );
      }
    },
    onSettled: (_data, _error, variables) => {
      if (automationId != null) {
        queryClient.invalidateQueries({ queryKey: ["threads", automationId, "chat"] });
      }
      if (variables) {
        queryClient.invalidateQueries({ queryKey: ["thread-messages", variables.threadId] });
      }
    },
  });

  return {
    sendMutation,
    renameThreadMutation,
  };
}
