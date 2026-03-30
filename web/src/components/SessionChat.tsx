/**
 * SessionChat - Interactive chat with Claude Code sessions via timeline drop-in.
 *
 * Features:
 * - Streaming assistant response via SSE
 * - Cancel button with AbortController
 * - Lock status indicators
 * - Error handling with retry
 */

import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { buildUrl } from "../services/api/base";
import { fetchWithRefresh } from "../lib/auth-refresh";
import { consumeSessionChatSseBuffer, flushSessionChatSseBuffer } from "../lib/sessionChatSse";
import { fetchSessionLockStatus, type SessionLockInfo } from "../services/api";
import type { AgentSession } from "../services/api/agents";
import { Badge, Button, Spinner } from "./ui";
import "../styles/session-chat.css";

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
  session_id?: string;
  source_session_id?: string;
  shipped_session_id?: string | null;
  created_continuation?: boolean;
  branched_from_event_id?: number | null;
  exit_code: number;
  total_text_length: number;
  persisted_events?: number;
  persistence_error?: string | null;
  sync_status?: "pending" | "complete" | "failed";
  control_status?: "completed" | "needs_user" | "blocked" | "failed";
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
  session: SessionChatTarget;
  onClose?: () => void;
  emptyStateTitle?: string;
  hintText?: string;
  composerPlaceholder?: string;
  onSessionChanged?: (nextSessionId: string, createdContinuation: boolean) => void;
  layout?: "panel" | "dock";
  dockHeaderStyle?: "callout" | "divider" | "hidden";
  introEyebrow?: string;
  introTitle?: string;
  introDescription?: string;
  submitLabel?: string;
  requireClickForFirstSend?: boolean;
  keyboardHintText?: string;
  /** Managed-local sessions use fire-and-forget dispatch (response arrives via SSE stream). */
  chatMode?: "cloud" | "managed_local";
}

export type SessionChatTarget = Pick<AgentSession, "id" | "project" | "provider">;

function getToolPrefix(toolNotices?: string[]): string {
  return toolNotices?.length ? toolNotices.join("\n") + "\n\n" : "";
}

function getAssistantText(message: ChatMessage): string {
  const toolPrefix = getToolPrefix(message.toolNotices);
  return message.content.startsWith(toolPrefix) ? message.content.slice(toolPrefix.length).trim() : message.content.trim();
}

function getSyncPendingPlaceholder(controlStatus?: SSEDone["control_status"]): string {
  if (controlStatus === "needs_user") {
    return "Waiting locally. Transcript syncing...";
  }

  return "Completed locally. Transcript syncing...";
}

export function SessionChat({
  session,
  onClose,
  emptyStateTitle,
  hintText,
  composerPlaceholder,
  onSessionChanged,
  layout = "panel",
  dockHeaderStyle = "callout",
  introEyebrow,
  introTitle,
  introDescription,
  submitLabel = "Send",
  requireClickForFirstSend = false,
  keyboardHintText,
  chatMode = "cloud",
}: SessionChatProps) {
  const isDock = layout === "dock";
  const isManagedLocal = chatMode === "managed_local";
  const queryClient = useQueryClient();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [blockedKeyboardSubmit, setBlockedKeyboardSubmit] = useState(false);

  const [sentConfirmation, setSentConfirmation] = useState(false);
  const [pendingManagedLocalMessage, setPendingManagedLocalMessage] = useState<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const dockTextareaRef = useRef<HTMLTextAreaElement>(null);
  const sentConfirmationTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const autoResizeDockTextarea = useCallback((el: HTMLTextAreaElement) => {
    el.style.height = "auto";
    if (el.scrollHeight > 0) {
      el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
    }
  }, []);

  // Scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    return () => {
      abortControllerRef.current?.abort();
      abortControllerRef.current = null;
      if (sentConfirmationTimerRef.current) clearTimeout(sentConfirmationTimerRef.current);
    };
  }, []);

  const handleDraftChange = useCallback((nextDraft: string) => {
    setDraft(nextDraft);
    if (!nextDraft.trim()) {
      setBlockedKeyboardSubmit(false);
      if (dockTextareaRef.current) {
        dockTextareaRef.current.style.height = "auto";
      }
    }
  }, []);

  const refreshCurrentSessionWorkspace = useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["session-lock", session.id] }),
      queryClient.invalidateQueries({ queryKey: ["agent-session-workspace", session.id] }),
      queryClient.invalidateQueries({ queryKey: ["agent-session", session.id] }),
      queryClient.invalidateQueries({ queryKey: ["agent-session-thread", session.id] }),
      queryClient.invalidateQueries({ queryKey: ["agent-session-projection-infinite", session.id] }),
      queryClient.invalidateQueries({ queryKey: ["agent-session-events", session.id] }),
      queryClient.invalidateQueries({ queryKey: ["agent-session-events-infinite", session.id] }),
      queryClient.invalidateQueries({ queryKey: ["agent-sessions"] }),
    ]);
  }, [queryClient, session.id]);

  const handleSSEEvent = useCallback(
    (eventType: string, data: unknown, assistantId: string) => {
      switch (eventType) {
        case "assistant_delta": {
          const delta = data as SSEAssistantDelta;
          // Preserve tool notices by prepending them to accumulated text
          setMessages((prev) =>
            prev.map((m) => {
              if (m.id !== assistantId) return m;
              const toolPrefix = getToolPrefix(m.toolNotices);
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
              const toolPrefix = getToolPrefix(toolNotices);
              // Update content to include the new notice
              // Extract the accumulated text (content without tool prefix)
              const existingToolPrefix = getToolPrefix(m.toolNotices);
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
          const done = data as SSEDone;
          const persistedEvents = done.persisted_events ?? 0;
          const syncStatus =
            done.sync_status ??
            (done.persistence_error ? "failed" : persistedEvents > 0 ? "complete" : undefined);
          const isPendingSync = syncStatus === "pending";
          const isPersisted = syncStatus === "complete" || (!syncStatus && persistedEvents > 0);
          const hasPersistenceFailure = Boolean(done.persistence_error) && !isPendingSync;

          if (hasPersistenceFailure && done.persistence_error) {
            setError(done.persistence_error);
          }

          if (isPendingSync) {
            setMessages((prev) =>
              prev.map((m) => {
                if (m.id !== assistantId || getAssistantText(m)) {
                  return m;
                }

                return {
                  ...m,
                  content: getToolPrefix(m.toolNotices) + getSyncPendingPlaceholder(done.control_status),
                };
              }),
            );
          }

          const nextSessionId = done.shipped_session_id;
          if (nextSessionId && isPersisted && !hasPersistenceFailure) {
            if (nextSessionId === session.id) {
              void refreshCurrentSessionWorkspace().finally(() => {
                setMessages([]);
              });
            } else {
              onSessionChanged?.(nextSessionId, Boolean(done.created_continuation));
            }
          }
          break;
        }
        default:
          // system, tool_result - ignore for now
          break;
      }
    },
    [onSessionChanged, refreshCurrentSessionWorkspace, session.id],
  );

  const lockStatusQuery = useQuery<SessionLockInfo | null>({
    queryKey: ["session-lock", session.id],
    queryFn: async () => {
      try {
        return await fetchSessionLockStatus(session.id);
      } catch {
        return null;
      }
    },
    enabled: Boolean(session.id),
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => (query.state.data?.locked ? 2_000 : false),
    staleTime: 15_000,
  });

  const lockInfo = useMemo(
    () =>
      lockStatusQuery.data
        ? {
            locked: lockStatusQuery.data.locked,
            holder: lockStatusQuery.data.holder ?? undefined,
            timeRemaining: lockStatusQuery.data.time_remaining_seconds ?? undefined,
          }
        : null,
    [lockStatusQuery.data],
  );

  const handleCancel = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setIsStreaming(false);
  }, []);

  const handleManagedLocalSend = useCallback(
    async (message: string) => {
      setPendingManagedLocalMessage(message);
      setIsSubmitting(true);
      try {
        const response = await fetchWithRefresh(buildUrl(`/sessions/${session.id}/chat`), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message }),
          credentials: "include",
        });

        if (!response.ok) {
          const errorData = await response.json().catch(() => ({}));
          if (response.status === 409) {
            const lockData = errorData?.detail?.lock_info;
            queryClient.setQueryData<SessionLockInfo | null>(["session-lock", session.id], {
              locked: true,
              holder: lockData?.holder,
              time_remaining_seconds: lockData?.time_remaining_seconds,
              fork_available: lockData?.fork_available,
            });
            throw new Error("Session is currently in use by another request");
          }
          throw new Error(errorData?.error || errorData?.detail || `Request failed: ${response.status}`);
        }

        const result = await response.json();
        if (!result.accepted) {
          throw new Error(result.error || "Session did not accept the message");
        }

        queryClient.setQueryData<SessionLockInfo | null>(["session-lock", session.id], {
          locked: true,
          holder: result.request_id ?? null,
          time_remaining_seconds: null,
          fork_available: true,
        });

        // Show brief "Sent" confirmation near the compose button.
        if (sentConfirmationTimerRef.current) clearTimeout(sentConfirmationTimerRef.current);
        setSentConfirmation(true);
        sentConfirmationTimerRef.current = setTimeout(() => setSentConfirmation(false), 2000);

        void refreshCurrentSessionWorkspace().finally(() => setPendingManagedLocalMessage(null));
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unknown error");
        setPendingManagedLocalMessage(null);
      } finally {
        setIsSubmitting(false);
      }
    },
    [queryClient, session.id, refreshCurrentSessionWorkspace],
  );

  const handleCloudSend = useCallback(
    async (message: string) => {
      const assistantId = `assistant-${Date.now()}`;
      setMessages((prev) => [
        ...prev,
        { id: assistantId, role: "assistant", content: "", timestamp: new Date(), isStreaming: true },
      ]);

      setIsSubmitting(true);
      setIsStreaming(true);
      abortControllerRef.current = new AbortController();

      try {
        const response = await fetchWithRefresh(buildUrl(`/sessions/${session.id}/chat`), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message }),
          credentials: "include",
          signal: abortControllerRef.current.signal,
        });

        if (!response.ok) {
          const errorData = await response.json().catch(() => ({}));
          if (response.status === 409) {
            const lockData = errorData?.detail?.lock_info;
            queryClient.setQueryData<SessionLockInfo | null>(["session-lock", session.id], {
              locked: true,
              holder: lockData?.holder,
              time_remaining_seconds: lockData?.time_remaining_seconds,
              fork_available: lockData?.fork_available,
            });
            throw new Error("Session is currently in use by another request");
          }
          throw new Error(errorData?.detail || `Request failed: ${response.status}`);
        }

        const reader = response.body?.getReader();
        if (!reader) throw new Error("No response body");

        const decoder = new TextDecoder();
        let buffer = "";

        const processEvent = ({ eventType, data }: { eventType: string; data: string }) => {
          try {
            const parsed = JSON.parse(data);
            handleSSEEvent(eventType, parsed, assistantId);
          } catch {
            // Ignore parse errors
          }
        };

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer = consumeSessionChatSseBuffer(
            buffer,
            decoder.decode(value, { stream: true }),
            processEvent,
          );
        }
        buffer = consumeSessionChatSseBuffer(buffer, decoder.decode(), processEvent);
        flushSessionChatSseBuffer(buffer, processEvent);
      } catch (e) {
        if (e instanceof Error && e.name === "AbortError") {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId ? { ...m, content: m.content + "\n\n[Cancelled]", isStreaming: false } : m,
            ),
          );
        } else {
          setError(e instanceof Error ? e.message : "Unknown error");
          setMessages((prev) => prev.filter((m) => m.id !== assistantId || m.content.length > 0));
        }
      } finally {
        setIsSubmitting(false);
        setIsStreaming(false);
        abortControllerRef.current = null;
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantId ? { ...m, isStreaming: false } : m)),
        );
      }
    },
    [handleSSEEvent, queryClient, session.id],
  );

  const handleSend = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();

      const message = draft.trim();
      if (!message || isSubmitting) return;

      setDraft("");
      setError(null);
      setBlockedKeyboardSubmit(false);

      if (!isManagedLocal) {
        setMessages((prev) => [
          ...prev,
          { id: `user-${Date.now()}`, role: "user", content: message, timestamp: new Date() },
        ]);
      }

      if (isManagedLocal) {
        await handleManagedLocalSend(message);
      } else {
        await handleCloudSend(message);
      }
    },
    [draft, isSubmitting, isManagedLocal, handleManagedLocalSend, handleCloudSend],
  );

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (
        requireClickForFirstSend &&
        messages.length === 0 &&
        draft.trim() &&
        !blockedKeyboardSubmit
      ) {
        setBlockedKeyboardSubmit(true);
        return;
      }
      handleSend(e as unknown as FormEvent);
    }
  };

  const statusBadge = isStreaming
    ? { variant: "success" as const, label: "Streaming" }
    : isSubmitting
      ? { variant: "warning" as const, label: "Sending" }
    : lockInfo?.locked
      ? { variant: "warning" as const, label: "Locked" }
      : { variant: "neutral" as const, label: "Ready" };

  const renderMessages = () =>
    messages.map((msg) => (
      <div key={msg.id} className={`session-chat-message session-chat-message--${msg.role}`}>
        <div className="session-chat-message-role">{msg.role}</div>
        <div className="session-chat-message-content">
          {msg.content || (msg.isStreaming ? <Spinner size="sm" /> : null)}
        </div>
      </div>
    ));

  return (
    <div
      className={`session-chat${isDock ? " session-chat--dock" : ""}`}
      data-testid={isDock ? "session-continuation-panel" : undefined}
    >
      {isDock ? (
        dockHeaderStyle === "hidden" ? null : dockHeaderStyle === "divider" ? (
          <div className="session-chat-divider" data-testid="session-chat-divider">
            <div className="session-chat-divider__copy">
              <div className="session-chat-divider__rule" />
              <div className="session-chat-divider__body">
                {introEyebrow ? (
                  <div className="session-chat-divider__eyebrow">{introEyebrow}</div>
                ) : null}
                {introTitle ? <div className="session-chat-divider__title">{introTitle}</div> : null}
                {introDescription ? (
                  <p className="session-chat-divider__description">{introDescription}</p>
                ) : null}
                {hintText ? <p className="session-chat-divider__hint">{hintText}</p> : null}
              </div>
            </div>
            <div className="session-chat-status">
              <Badge variant={statusBadge.variant}>{statusBadge.label}</Badge>
            </div>
          </div>
        ) : (
          <div
            className={`session-chat-callout${requireClickForFirstSend ? " session-chat-callout--branching" : ""}`}
          >
            <div className="session-chat-callout__copy">
              {introEyebrow ? (
                <div className="session-chat-callout__eyebrow">{introEyebrow}</div>
              ) : null}
              {introTitle ? <div className="session-chat-callout__title">{introTitle}</div> : null}
              {introDescription ? (
                <p className="session-chat-callout__description">{introDescription}</p>
              ) : null}
              {hintText ? <p className="session-chat-callout__hint">{hintText}</p> : null}
            </div>
            <div className="session-chat-status">
              <Badge variant={statusBadge.variant}>{statusBadge.label}</Badge>
            </div>
          </div>
        )
      ) : (
        <div className="session-chat-header">
          <div className="session-chat-info">
            {onClose && (
              <button
                type="button"
                className="session-chat-back"
                onClick={onClose}
                title="Back to details"
              >
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                >
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
            <Badge variant={statusBadge.variant}>{statusBadge.label}</Badge>
          </div>
        </div>
      )}

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

      {isDock && isManagedLocal ? null : isDock ? (
        <div className="session-chat-messages session-chat-messages--dock">
          {messages.length > 0 ? (
            <>
              {renderMessages()}
              <div ref={messagesEndRef} />
            </>
          ) : null}
        </div>
      ) : (
        <div className="session-chat-messages">
          {messages.length === 0 ? (
            <div className="session-chat-empty">
              <p>{emptyStateTitle || "Start a conversation with this session."}</p>
              <p className="session-chat-hint">
                {hintText || "Context from previous turns will be preserved via --resume."}
              </p>
            </div>
          ) : (
            renderMessages()
          )}
          <div ref={messagesEndRef} />
        </div>
      )}

      <form
        className={`session-chat-composer${isDock ? " session-chat-composer--dock" : ""}`}
        onSubmit={handleSend}
      >
        {blockedKeyboardSubmit ? (
          <div className="session-chat-confirmation" data-testid="session-chat-explicit-submit-hint">
            {keyboardHintText || `Click "${submitLabel}" to confirm the first cloud message.`}
          </div>
        ) : isDock ? null : (
          <div className="session-chat-confirmation session-chat-confirmation--spacer" aria-hidden="true" />
        )}
        {isManagedLocal && pendingManagedLocalMessage ? (
          <div className="session-chat-pending-message">
            <span className="session-chat-pending-message__text">{pendingManagedLocalMessage}</span>
            <span className="session-chat-pending-message__spinner" aria-label="Sending" />
          </div>
        ) : null}
        {isDock ? (
          <div className="session-chat-composer-row">
            <textarea
              ref={dockTextareaRef}
              value={draft}
              onChange={(e) => {
                handleDraftChange(e.target.value);
                autoResizeDockTextarea(e.target);
              }}
              onKeyDown={handleKeyDown}
              placeholder={composerPlaceholder || "Type a message..."}
              disabled={isSubmitting || lockInfo?.locked}
              rows={1}
            />
            {isManagedLocal && sentConfirmation ? (
              <span className="session-chat-sent-notice">Sent</span>
            ) : null}
            {isStreaming ? (
              <Button type="button" variant="secondary" size="sm" onClick={handleCancel}>
                Cancel
              </Button>
            ) : (
              <Button
                type="submit"
                variant="primary"
                size="sm"
                disabled={!draft.trim() || isSubmitting || lockInfo?.locked}
              >
                {submitLabel}
              </Button>
            )}
          </div>
        ) : (
          <>
            <textarea
              value={draft}
              onChange={(e) => handleDraftChange(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={composerPlaceholder || "Type a message..."}
              disabled={isSubmitting || lockInfo?.locked}
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
                  disabled={!draft.trim() || isSubmitting || lockInfo?.locked}
                >
                  {submitLabel}
                </Button>
              )}
            </div>
          </>
        )}
      </form>
    </div>
  );
}
