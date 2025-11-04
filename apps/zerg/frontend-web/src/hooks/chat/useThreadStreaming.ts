import { useCallback, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useWebSocket } from "../../lib/useWebSocket";

interface StreamingState {
  streamingMessages: Map<number, string>;
  streamingMessageId: number | null;
  pendingTokenBuffer: string;
}

interface UseThreadStreamingParams {
  agentId: number | null;
  effectiveThreadId: number | null;
}

export function useThreadStreaming({ agentId, effectiveThreadId }: UseThreadStreamingParams) {
  const queryClient = useQueryClient();
  const tokenCountRef = useRef(0);
  const streamStartTimeRef = useRef<number>(0);

  const [streamingState, setStreamingState] = useState<StreamingState>({
    streamingMessages: new Map(),
    streamingMessageId: null,
    pendingTokenBuffer: "",
  });

  const wsQueries = useMemo(() => {
    const queries = [];
    if (agentId != null) {
      queries.push(["threads", agentId, "chat"]);
      queries.push(["threads", agentId, "automation"]);
    }
    if (effectiveThreadId != null) {
      queries.push(["thread-messages", effectiveThreadId]);
    }
    return queries;
  }, [agentId, effectiveThreadId]);

  const handleStreamingMessage = useCallback((envelope: any) => {
    const { type, data } = envelope;

    if (type === "stream_start") {
      console.log('[CHAT] 🎬 STREAM_START');
      tokenCountRef.current = 0;
      streamStartTimeRef.current = Date.now();
      setStreamingState({
        streamingMessages: new Map(),
        streamingMessageId: null,
        pendingTokenBuffer: "",
      });
    } else if (type === "stream_chunk") {
      if (data.chunk_type === "assistant_token") {
        const token = data.content || "";
        tokenCountRef.current++;

        // Sample logging: first token + every 50th token
        if (tokenCountRef.current === 1 || tokenCountRef.current % 50 === 0) {
          console.log(`[CHAT] 🔤 Token #${tokenCountRef.current}`);
        }

        setStreamingState(prev => {
          if (prev.streamingMessageId) {
            // Have ID, accumulate normally
            const next = new Map(prev.streamingMessages);
            const current = next.get(prev.streamingMessageId) || "";
            next.set(prev.streamingMessageId, current + token);
            return {
              ...prev,
              streamingMessages: next,
            };
          } else {
            // No ID yet, buffer tokens that arrive before assistant_id
            return {
              ...prev,
              pendingTokenBuffer: prev.pendingTokenBuffer + token,
            };
          }
        });
      }
    } else if (type === "assistant_id") {
      console.log('[CHAT] 🆔 ASSISTANT_ID:', data.message_id);
      setStreamingState(prev => {
        const next = new Map(prev.streamingMessages);
        next.set(data.message_id, prev.pendingTokenBuffer);
        return {
          streamingMessages: next,
          streamingMessageId: data.message_id,
          pendingTokenBuffer: prev.pendingTokenBuffer, // Keep pendingTokenBuffer visible until stream_end
        };
      });
    } else if (type === "stream_end") {
      const duration = Date.now() - streamStartTimeRef.current;
      console.log(`[CHAT] 🏁 STREAM_END - ${tokenCountRef.current} tokens in ${duration}ms`);
      // Finalize: refresh messages from API
      if (data.thread_id === effectiveThreadId) {
        queryClient.invalidateQueries({
          queryKey: ["thread-messages", data.thread_id]
        });
      }
      // Also refresh thread list to update previews for the thread that got new messages
      if (agentId != null) {
        queryClient.invalidateQueries({
          queryKey: ["threads", agentId, "chat"]
        });
      }
      setStreamingState({
        streamingMessages: new Map(),
        streamingMessageId: null,
        pendingTokenBuffer: "",
      });
    }
  }, [effectiveThreadId, agentId, queryClient]);

  const { sendMessage: wsSendMessage } = useWebSocket(agentId != null, {
    includeAuth: true,
    invalidateQueries: wsQueries,
    onStreamingMessage: handleStreamingMessage,
  });

  const subscribe = useCallback(() => {
    if (effectiveThreadId && wsSendMessage) {
      console.log('[CHAT] 📡 Subscribing to thread:', effectiveThreadId);
      wsSendMessage({
        type: "subscribe_thread",
        thread_id: effectiveThreadId,
        message_id: `sub-${Date.now()}`,
      });
    }
  }, [effectiveThreadId, wsSendMessage]);

  return {
    ...streamingState,
    subscribe,
  };
}
