import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useWebSocket } from "../../lib/useWebSocket";

interface StreamingState {
  streamingMessages: Map<number, string>;
  streamingMessageId: number | null;
  pendingTokenBuffer: string;
  streamingThreadId: number | null;
}

interface UseThreadStreamingParams {
  agentId: number | null;
  effectiveThreadId: number | null;
}

export function useThreadStreaming({ agentId, effectiveThreadId }: UseThreadStreamingParams) {
  const queryClient = useQueryClient();
  const tokenCountRef = useRef(0);
  const streamStartTimeRef = useRef<number>(0);
  const streamingThreadIdRef = useRef<number | null>(null);

  const [streamingState, setStreamingState] = useState<StreamingState>({
    streamingMessages: new Map(),
    streamingMessageId: null,
    pendingTokenBuffer: "",
    streamingThreadId: null,
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
      const streamThreadId = data.thread_id;
      console.log('[CHAT] 🎬 STREAM_START for thread:', streamThreadId, '(current:', effectiveThreadId, ')');
      tokenCountRef.current = 0;
      streamStartTimeRef.current = Date.now();

      // Track the streaming thread ID in a ref to avoid stale closures
      streamingThreadIdRef.current = streamThreadId;

      // Only process streaming if it belongs to the current active thread
      if (streamThreadId !== effectiveThreadId) {
        console.log(`[CHAT] ⚠️ Ignoring stream for thread ${streamThreadId} (current: ${effectiveThreadId})`);
        return;
      }

      setStreamingState({
        streamingMessages: new Map(),
        streamingMessageId: null,
        pendingTokenBuffer: "",
        streamingThreadId: streamThreadId,
      });
    } else if (type === "stream_chunk") {
      // Guard: Only process tokens if streaming belongs to current active thread
      // Use ref to avoid stale closure - always reads current value
      if (streamingThreadIdRef.current !== effectiveThreadId || streamingThreadIdRef.current === null) {
        if (streamingThreadIdRef.current !== null) {
          console.log(`[CHAT] ⚠️ Ignoring chunk for thread ${streamingThreadIdRef.current} (current: ${effectiveThreadId})`);
        }
        return;
      }
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
      // Guard: Only process assistant_id if streaming belongs to current active thread
      // Use ref to avoid stale closure
      if (streamingThreadIdRef.current !== effectiveThreadId || streamingThreadIdRef.current === null) {
        if (streamingThreadIdRef.current !== null) {
          console.log(`[CHAT] ⚠️ Ignoring assistant_id for thread ${streamingThreadIdRef.current} (current: ${effectiveThreadId})`);
        }
        return;
      }
      console.log('[CHAT] 🆔 ASSISTANT_ID:', data.message_id, 'for thread:', streamingThreadIdRef.current);
      setStreamingState(prev => {
        const next = new Map(prev.streamingMessages);
        next.set(data.message_id, prev.pendingTokenBuffer);
        return {
          streamingMessages: next,
          streamingMessageId: data.message_id,
          pendingTokenBuffer: prev.pendingTokenBuffer, // Keep pendingTokenBuffer visible until stream_end
          streamingThreadId: prev.streamingThreadId,
        };
      });
    } else if (type === "stream_end") {
      const duration = Date.now() - streamStartTimeRef.current;
      const endedThreadId = data.thread_id;
      const streamingOwner = streamingThreadIdRef.current;

      console.log(`[CHAT] 🏁 STREAM_END - ${tokenCountRef.current} tokens in ${duration}ms (thread: ${endedThreadId}, owner: ${streamingOwner})`);

      // Verify the stream_end matches our tracking
      if (streamingOwner !== endedThreadId) {
        console.warn(`[CHAT] ⚠️ MISMATCH: stream_end for thread ${endedThreadId} but tracking ${streamingOwner}`);
      }

      // Clear the streaming thread ID ref
      streamingThreadIdRef.current = null;

      // Finalize: refresh messages from API
      if (endedThreadId === effectiveThreadId) {
        queryClient.invalidateQueries({
          queryKey: ["thread-messages", endedThreadId]
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
        streamingThreadId: null,
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

  // Deterministic teardown: clear streaming if thread changes
  useEffect(() => {
    if (effectiveThreadId !== streamingThreadIdRef.current) {
      console.log('[CHAT] 🧹 Thread changed, clearing stream state');
      streamingThreadIdRef.current = null;
      setStreamingState({
        streamingMessages: new Map(),
        streamingMessageId: null,
        pendingTokenBuffer: "",
        streamingThreadId: null,
      });
    }
  }, [effectiveThreadId]);

  return {
    ...streamingState,
    subscribe,
  };
}
