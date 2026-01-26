import { useCallback, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useWebSocket } from "../../lib/useWebSocket";
import { logger } from "../../jarvis/core/logger";

interface StreamingState {
  streamingMessages: Map<number, string>;
  streamingMessageId: number | null;
  pendingTokenBuffer: string;
  tokenCount: number;
  startTime: number;
}

interface UseThreadStreamingParams {
  ficheId: number | null;
  effectiveThreadId: number | null;
}

export function useThreadStreaming({ ficheId, effectiveThreadId }: UseThreadStreamingParams) {
  const queryClient = useQueryClient();

  // Map of streaming state by thread ID - stores ALL concurrent streams
  const streamsByThread = useRef<Map<number, StreamingState>>(new Map());

  // Version counter to force re-renders when Map changes
  const [, setVersion] = useState(0);
  const forceUpdate = useCallback(() => setVersion(v => v + 1), []);

  const wsQueries = useMemo(() => {
    const queries = [];
    if (ficheId != null) {
      queries.push(["threads", ficheId, "chat"]);
      queries.push(["threads", ficheId, "automation"]);
    }
    if (effectiveThreadId != null) {
      queries.push(["thread-messages", effectiveThreadId]);
    }
    return queries;
  }, [ficheId, effectiveThreadId]);

  const handleStreamingMessage = useCallback((envelope: any) => {
    const { type, data } = envelope;

    if (type === "stream_start") {
      const threadId = data.thread_id;
      logger.debug(`[Chat] Stream started for thread: ${threadId}`);

      // Initialize new stream state for this thread
      streamsByThread.current.set(threadId, {
        streamingMessages: new Map(),
        streamingMessageId: null,
        pendingTokenBuffer: "",
        tokenCount: 0,
        startTime: Date.now(),
      });

      // Force re-render to show writing badge immediately
      forceUpdate();

    } else if (type === "stream_chunk") {
      // Accept ALL chunks - no filtering
      const threadId = data.thread_id;
      const stream = streamsByThread.current.get(threadId);

      if (!stream) {
        logger.warn(`[Chat] Received chunk for unknown thread ${threadId}`);
        return;
      }

      if (data.chunk_type === "assistant_token") {
        const token = data.content || "";
        stream.tokenCount++;

        // Sample logging: first token + every 50th token (verbose mode only)
        if (stream.tokenCount === 1 || stream.tokenCount % 50 === 0) {
          logger.debug(`[Chat] Thread ${threadId} token #${stream.tokenCount}`);
        }

        if (stream.streamingMessageId) {
          // Have ID, accumulate normally
          const current = stream.streamingMessages.get(stream.streamingMessageId) || "";
          stream.streamingMessages.set(stream.streamingMessageId, current + token);
        } else {
          // No ID yet, buffer tokens
          stream.pendingTokenBuffer += token;
        }

        // Force re-render to update UI (active thread tokens + badge indicators)
        forceUpdate();
      }

    } else if (type === "assistant_id") {
      const threadId = data.thread_id;
      const stream = streamsByThread.current.get(threadId);

      if (!stream) {
        logger.warn(`[Chat] Received assistant_id for unknown thread ${threadId}`);
        return;
      }

      logger.debug(`[Chat] Assistant ID: ${data.message_id} for thread: ${threadId}`);
      stream.streamingMessageId = data.message_id;
      stream.streamingMessages.set(data.message_id, stream.pendingTokenBuffer);

      // Force re-render to update UI
      forceUpdate();

    } else if (type === "stream_end") {
      const threadId = data.thread_id;
      const stream = streamsByThread.current.get(threadId);

      if (stream) {
        const duration = Date.now() - stream.startTime;
        logger.debug(`[Chat] Stream ended - thread ${threadId}: ${stream.tokenCount} tokens in ${duration}ms`);
      }

      // Refresh messages from API for this thread
      queryClient.invalidateQueries({
        queryKey: ["thread-messages", threadId]
      });

      // Also refresh thread list to update previews
      if (ficheId != null) {
        queryClient.invalidateQueries({
          queryKey: ["threads", ficheId, "chat"]
        });
      }

      // Clear stream state for this thread
      streamsByThread.current.delete(threadId);

      // Force re-render to update UI (clear active stream + remove badge)
      forceUpdate();
    }
  }, [ficheId, queryClient, forceUpdate]);

  useWebSocket(ficheId != null, {
    includeAuth: true,
    invalidateQueries: wsQueries,
    onStreamingMessage: handleStreamingMessage,
  });

  // Get the active thread's streaming state
  const activeStream = effectiveThreadId != null
    ? streamsByThread.current.get(effectiveThreadId)
    : null;

  // Return active thread's stream data + list of all streaming threads
  return {
    streamingMessages: activeStream?.streamingMessages || new Map(),
    streamingMessageId: activeStream?.streamingMessageId || null,
    pendingTokenBuffer: activeStream?.pendingTokenBuffer || "",
    allStreamingThreadIds: Array.from(streamsByThread.current.keys()),
  };
}
