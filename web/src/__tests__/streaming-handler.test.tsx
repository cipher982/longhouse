/**
 * Unit test for streaming message handler logic
 *
 * This validates the exact event sequence that causes the bug:
 * tokens arriving before assistant_id
 */

import { renderHook, act } from '@testing-library/react';
import { useState, useCallback } from 'react';
import { describe, it, expect } from 'vitest';

// Simplified version of the streaming handler logic
function useStreamingHandler() {
  const [streamingMessages, setStreamingMessages] = useState<Map<number, string>>(new Map());
  const [streamingMessageId, setStreamingMessageId] = useState<number | null>(null);
  const [pendingTokenBuffer, setPendingTokenBuffer] = useState<string>("");

  const handleStreamingMessage = useCallback((envelope: any) => {
    const { type, data } = envelope;

    if (type === "stream_start") {
      setStreamingMessageId(null);
      setStreamingMessages(new Map());
      setPendingTokenBuffer("");
    } else if (type === "stream_chunk") {
      if (data.chunk_type === "assistant_token") {
        if (streamingMessageId) {
          setStreamingMessages(prev => {
            const next = new Map(prev);
            const current = next.get(streamingMessageId) || "";
            next.set(streamingMessageId, current + (data.content || ""));
            return next;
          });
        } else {
          setPendingTokenBuffer(prev => prev + (data.content || ""));
        }
      }
    } else if (type === "assistant_id") {
      setStreamingMessageId(data.message_id);
      setStreamingMessages(prev => {
        const next = new Map(prev);
        next.set(data.message_id, pendingTokenBuffer);
        return next;
      });
      setPendingTokenBuffer("");
    } else if (type === "stream_end") {
      setStreamingMessageId(null);
      setStreamingMessages(new Map());
      setPendingTokenBuffer("");
    }
  }, [streamingMessageId, pendingTokenBuffer]);

  const getVisibleContent = useCallback(() => {
    if (streamingMessageId) {
      return streamingMessages.get(streamingMessageId) || "";
    }
    return pendingTokenBuffer;
  }, [streamingMessageId, streamingMessages, pendingTokenBuffer]);

  return {
    handleStreamingMessage,
    getVisibleContent,
    streamingMessageId,
    pendingTokenBuffer,
  };
}

describe('Streaming Message Handler', () => {
  it('buffers tokens BEFORE assistant_id arrives (BUG FIX)', () => {
    const { result } = renderHook(() => useStreamingHandler());

    // 1. Stream starts
    act(() => {
      result.current.handleStreamingMessage({
        type: 'stream_start',
        data: { thread_id: 1 }
      });
    });

    // 2. Tokens arrive BEFORE assistant_id (this is the bug scenario)
    act(() => {
      result.current.handleStreamingMessage({
        type: 'stream_chunk',
        data: { chunk_type: 'assistant_token', content: 'Hello' }
      });
    });

    // CRITICAL: Content should be visible even without ID
    expect(result.current.getVisibleContent()).toBe('Hello');
    expect(result.current.pendingTokenBuffer).toBe('Hello');
    expect(result.current.streamingMessageId).toBeNull();

    // 3. More tokens
    act(() => {
      result.current.handleStreamingMessage({
        type: 'stream_chunk',
        data: { chunk_type: 'assistant_token', content: ' ' }
      });
    });
    act(() => {
      result.current.handleStreamingMessage({
        type: 'stream_chunk',
        data: { chunk_type: 'assistant_token', content: 'world' }
      });
    });

    // Should accumulate in buffer
    expect(result.current.getVisibleContent()).toBe('Hello world');
    expect(result.current.pendingTokenBuffer).toBe('Hello world');

    // 4. assistant_id arrives (late)
    act(() => {
      result.current.handleStreamingMessage({
        type: 'assistant_id',
        data: { message_id: 123 }
      });
    });

    // Content should move to streaming messages
    expect(result.current.streamingMessageId).toBe(123);
    expect(result.current.getVisibleContent()).toBe('Hello world');
    expect(result.current.pendingTokenBuffer).toBe('');

    // 5. More tokens after ID (normal flow)
    act(() => {
      result.current.handleStreamingMessage({
        type: 'stream_chunk',
        data: { chunk_type: 'assistant_token', content: '!' }
      });
    });

    expect(result.current.getVisibleContent()).toBe('Hello world!');

    // 6. Stream ends
    act(() => {
      result.current.handleStreamingMessage({
        type: 'stream_end',
        data: { thread_id: 1 }
      });
    });

    expect(result.current.streamingMessageId).toBeNull();
    expect(result.current.pendingTokenBuffer).toBe('');
  });

  it('handles normal flow (assistant_id before tokens)', () => {
    const { result } = renderHook(() => useStreamingHandler());

    act(() => result.current.handleStreamingMessage({
      type: 'stream_start',
      data: { thread_id: 1 }
    }));

    // If assistant_id arrives first (unlikely but possible)
    act(() => result.current.handleStreamingMessage({
      type: 'assistant_id',
      data: { message_id: 123 }
    }));

    expect(result.current.streamingMessageId).toBe(123);

    // Then tokens
    act(() => result.current.handleStreamingMessage({
      type: 'stream_chunk',
      data: { chunk_type: 'assistant_token', content: 'Test' }
    }));

    expect(result.current.getVisibleContent()).toBe('Test');
  });
});
