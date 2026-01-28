/**
 * SessionChat - Interactive chat with Claude Code sessions via Forum drop-in.
 *
 * Features:
 * - Streaming assistant response via SSE
 * - Cancel button with AbortController
 * - Lock status indicators
 * - Error handling with retry
 */

import { useCallback, useRef, useState, type FormEvent, useEffect } from "react";
import { buildUrl } from "../services/api/base";
import { Badge, Button, Spinner } from "./ui";
import type { ActiveSession } from "../hooks/useActiveSessions";

// SSE Event types from backend
interface SSESystemEvent {
  type: string;
  session_id?: string;
  provider_session_id?: string;
  workspace?: string;
  timestamp?: string;
}

interface SSEAssistantDelta {
  text: string;
  accumulated: string;
}

interface SSEToolUse {
  name: string;
  id: string;
}

interface SSEError {
  error: string;
  details?: string;
}

interface SSEDone {
  exit_code: number;
  total_text_length: number;
  timestamp: string;
}

// Message for display
interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: Date;
  isStreaming?: boolean;
  toolNotices?: string[]; // Track tool notices separately to avoid overwrite
}

interface SessionChatProps {
  session: ActiveSession;
  onClose?: () => void;
}

export function SessionChat({ session, onClose }: SessionChatProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lockInfo, setLockInfo] = useState<{
    locked: boolean;
    holder?: string;
    timeRemaining?: number;
  } | null>(null);

  const abortControllerRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Check lock status on mount
  useEffect(() => {
    const checkLock = async () => {
      try {
        const response = await fetch(buildUrl(`/sessions/${session.id}/lock`), {
          credentials: "include",
        });
        if (response.ok) {
          const data = await response.json();
          setLockInfo(data);
        }
      } catch (e) {
        // Ignore lock check errors
      }
    };
    checkLock();
  }, [session.id]);

  const handleCancel = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setIsStreaming(false);
  }, []);

  const handleSend = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();

      const message = draft.trim();
      if (!message || isStreaming) return;

      setDraft("");
      setError(null);

      // Add user message
      const userMessage: ChatMessage = {
        id: `user-${Date.now()}`,
        role: "user",
        content: message,
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, userMessage]);

      // Create placeholder for assistant response
      const assistantId = `assistant-${Date.now()}`;
      const assistantMessage: ChatMessage = {
        id: assistantId,
        role: "assistant",
        content: "",
        timestamp: new Date(),
        isStreaming: true,
      };
      setMessages((prev) => [...prev, assistantMessage]);

      // Start streaming
      setIsStreaming(true);
      abortControllerRef.current = new AbortController();

      try {
        const response = await fetch(buildUrl(`/sessions/${session.id}/chat`), {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ message }),
          credentials: "include",
          signal: abortControllerRef.current.signal,
        });

        if (!response.ok) {
          const errorData = await response.json().catch(() => ({}));

          if (response.status === 409) {
            // Session locked - backend nests lock_info under detail.lock_info
            const lockData = errorData?.detail?.lock_info;
            setLockInfo({
              locked: true,
              holder: lockData?.holder,
              timeRemaining: lockData?.time_remaining_seconds,
            });
            throw new Error("Session is currently in use by another request");
          }

          throw new Error(errorData?.detail || `Request failed: ${response.status}`);
        }

        // Process SSE stream
        const reader = response.body?.getReader();
        if (!reader) throw new Error("No response body");

        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });

          // Parse SSE events
          const lines = buffer.split("\n");
          buffer = lines.pop() || ""; // Keep incomplete line in buffer

          let eventType = "";
          for (const line of lines) {
            if (line.startsWith("event: ")) {
              eventType = line.slice(7);
            } else if (line.startsWith("data: ")) {
              const data = line.slice(6);
              try {
                const parsed = JSON.parse(data);
                handleSSEEvent(eventType, parsed, assistantId);
              } catch {
                // Ignore parse errors
              }
            }
          }
        }
      } catch (e) {
        if (e instanceof Error && e.name === "AbortError") {
          // Cancelled by user
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId ? { ...m, content: m.content + "\n\n[Cancelled]", isStreaming: false } : m,
            ),
          );
        } else {
          setError(e instanceof Error ? e.message : "Unknown error");
          // Remove empty assistant message on error
          setMessages((prev) => prev.filter((m) => m.id !== assistantId || m.content.length > 0));
        }
      } finally {
        setIsStreaming(false);
        abortControllerRef.current = null;

        // Mark message as done streaming
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantId ? { ...m, isStreaming: false } : m)),
        );
      }
    },
    [draft, isStreaming, session.id],
  );

  const handleSSEEvent = useCallback(
    (eventType: string, data: unknown, assistantId: string) => {
      switch (eventType) {
        case "assistant_delta": {
          const delta = data as SSEAssistantDelta;
          // Preserve tool notices by prepending them to accumulated text
          setMessages((prev) =>
            prev.map((m) => {
              if (m.id !== assistantId) return m;
              const toolPrefix = m.toolNotices?.length
                ? m.toolNotices.join("\n") + "\n\n"
                : "";
              return { ...m, content: toolPrefix + delta.accumulated };
            }),
          );
          break;
        }
        case "tool_use": {
          const tool = data as SSEToolUse;
          // Track tool notices separately so they persist across assistant_delta overwrites
          setMessages((prev) =>
            prev.map((m) => {
              if (m.id !== assistantId) return m;
              const notice = `[Using tool: ${tool.name}]`;
              const toolNotices = [...(m.toolNotices || []), notice];
              const toolPrefix = toolNotices.join("\n") + "\n\n";
              // Update content to include the new notice
              // Extract the accumulated text (content without tool prefix)
              const existingToolPrefix = m.toolNotices?.length
                ? m.toolNotices.join("\n") + "\n\n"
                : "";
              const accumulatedText = m.content.startsWith(existingToolPrefix)
                ? m.content.slice(existingToolPrefix.length)
                : m.content;
              return { ...m, toolNotices, content: toolPrefix + accumulatedText };
            }),
          );
          break;
        }
        case "error": {
          const err = data as SSEError;
          setError(err.error);
          break;
        }
        case "done": {
          // Final cleanup handled in finally block
          break;
        }
        default:
          // system, tool_result - ignore for now
          break;
      }
    },
    [],
  );

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend(e as unknown as FormEvent);
    }
  };

  return (
    <div className="session-chat">
      <div className="session-chat-header">
        <div className="session-chat-info">
          {onClose && (
            <button
              type="button"
              className="session-chat-back"
              onClick={onClose}
              title="Back to details"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M19 12H5M12 19l-7-7 7-7" />
              </svg>
            </button>
          )}
          <div className="session-chat-titles">
            <span className="session-chat-title">{session.project || "Session"}</span>
            <span className="session-chat-provider">{session.provider}</span>
          </div>
        </div>
        <div className="session-chat-status">
          {isStreaming ? (
            <Badge variant="success">Streaming</Badge>
          ) : lockInfo?.locked ? (
            <Badge variant="warning">Locked</Badge>
          ) : (
            <Badge variant="neutral">Ready</Badge>
          )}
        </div>
      </div>

      {error && (
        <div className="session-chat-error">
          <span>{error}</span>
          <button type="button" onClick={() => setError(null)}>
            Dismiss
          </button>
        </div>
      )}

      {lockInfo?.locked && !isStreaming && (
        <div className="session-chat-lock-notice">
          <span>
            Session in use{lockInfo.holder ? ` by ${lockInfo.holder}` : ""}.
            {lockInfo.timeRemaining && ` ~${Math.ceil(lockInfo.timeRemaining)}s remaining.`}
          </span>
        </div>
      )}

      <div className="session-chat-messages">
        {messages.length === 0 ? (
          <div className="session-chat-empty">
            <p>Start a conversation with this session.</p>
            <p className="session-chat-hint">
              Context from previous turns will be preserved via --resume.
            </p>
          </div>
        ) : (
          messages.map((msg) => (
            <div key={msg.id} className={`session-chat-message session-chat-message--${msg.role}`}>
              <div className="session-chat-message-role">{msg.role}</div>
              <div className="session-chat-message-content">
                {msg.content || (msg.isStreaming ? <Spinner size="sm" /> : null)}
              </div>
            </div>
          ))
        )}
        <div ref={messagesEndRef} />
      </div>

      <form className="session-chat-composer" onSubmit={handleSend}>
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Type a message..."
          disabled={isStreaming || lockInfo?.locked}
          rows={2}
        />
        <div className="session-chat-actions">
          {isStreaming ? (
            <Button type="button" variant="secondary" size="sm" onClick={handleCancel}>
              Cancel
            </Button>
          ) : (
            <Button
              type="submit"
              variant="primary"
              size="sm"
              disabled={!draft.trim() || lockInfo?.locked}
            >
              Send
            </Button>
          )}
        </div>
      </form>
    </div>
  );
}
