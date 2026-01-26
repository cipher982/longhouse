import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { logger } from "../../jarvis/core/logger";
import {
  postThreadMessage,
  startThreadCourse,
  startWorkflowExecution,
  Thread,
  ThreadMessage,
  updateThread,
} from "../../services/api";

interface UseChatActionsParams {
  ficheId: number | null;
  effectiveThreadId: number | null;
}

export function useChatActions({ ficheId, effectiveThreadId }: UseChatActionsParams) {
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
      logger.debug(`[Chat] Triggering thread course: ${threadId}`);
      await startThreadCourse(threadId);
      logger.debug('[Chat] Course started');
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
      } as unknown as ThreadMessage;

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
      if (ficheId != null) {
        queryClient.invalidateQueries({ queryKey: ["threads", ficheId, "chat"] });
      }
    },
  });

  // Workflow execution mutation
  const executeWorkflowMutation = useMutation({
    mutationFn: ({ workflowId }: { workflowId: number }) => startWorkflowExecution(workflowId),
    onSuccess: (result) => {
      toast.success(`Workflow execution started! ID: ${result.execution_id}`);
      // Send a message to the chat about the workflow execution
      if (effectiveThreadId) {
        sendMutation.mutate({
          threadId: effectiveThreadId,
          content: `🔄 Started workflow execution #${result.execution_id} (Phase: ${result.phase})`,
        });
      }
    },
    onError: (error: Error) => {
      toast.error(`Failed to execute workflow: ${error.message}`);
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
      if (ficheId == null) {
        return {};
      }
      const queryKey = ["threads", ficheId, "chat"] as const;
      await queryClient.cancelQueries({ queryKey });
      const previousThreads = queryClient.getQueryData<Thread[]>(queryKey);
      queryClient.setQueryData<Thread[]>(queryKey, (old) =>
        old ? old.map((thread) => (thread.id === threadId ? { ...thread, title } : thread)) : old
      );
      return { previousThreads };
    },
    onError: (error, _variables, context) => {
      if (ficheId == null) {
        return;
      }
      if (context?.previousThreads) {
        queryClient.setQueryData(["threads", ficheId, "chat"], context.previousThreads);
      }
      toast.error("Failed to rename thread", {
        duration: 6000,
      });
    },
    onSuccess: (updatedThread) => {
      if (ficheId != null) {
        queryClient.setQueryData<Thread[]>(["threads", ficheId, "chat"], (old) =>
          old ? old.map((thread) => (thread.id === updatedThread.id ? updatedThread : thread)) : old
        );
      }
    },
    onSettled: (_data, _error, variables) => {
      if (ficheId != null) {
        queryClient.invalidateQueries({ queryKey: ["threads", ficheId, "chat"] });
      }
      if (variables) {
        queryClient.invalidateQueries({ queryKey: ["thread-messages", variables.threadId] });
      }
    },
  });

  return {
    sendMutation,
    executeWorkflowMutation,
    renameThreadMutation,
  };
}
