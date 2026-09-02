/**
 * SessionChat - Live-send dock for timeline sessions.
 *
 * Features:
 * - Lock status indicators
 * - Error handling with retry
 */

import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  cancelSessionInput,
  fetchSessionInputs,
  fetchSessionLockStatus,
  interruptLiveSession,
  interruptConsoleTurn,
  postSessionInput,
  postSessionInputMultipart,
  type QueuedInputSummary,
  type SessionLockInfo,
} from "../services/api";
import type { AgentSession } from "../services/api/agents";
import type { ManagedLaunchSuggestion, TimelineItem } from "../lib/sessionWorkspace";
import { useComposerAttachments } from "../lib/useComposerAttachments";
import { Badge, Button } from "./ui";
import { AttachmentTray } from "./AttachmentTray";
import { ManagedLaunchHintCard } from "./session-workspace/ManagedLaunchHintCard";
import { ProviderGlyph } from "./ProviderGlyph";
import { getProviderLabel } from "../lib/providers";
import "../styles/session-chat.css";

interface PendingManagedLocalInput {
  text: string;
  clientRequestId: string;
  serverInputId: number | null;
  intent: "auto" | "queue" | "steer";
  phase: "submitting";
}

interface SessionChatProps {
  session: SessionChatTarget;
  onClose?: () => void;
  emptyStateTitle?: string;
  hintText?: string;
  composerPlaceholder?: string;
  layout?: "panel" | "dock";
  submitLabel?: string;
  requireClickForFirstSend?: boolean;
  keyboardHintText?: string;
  /** Managed-local sessions use explicit live-send with fast JSON ack. */
  chatMode?: "managed_local";
  composerDisabledReason?: string | null;
  /// Heading over the disabled-composer copy. Derived from the typed blocker;
  /// falls back only when a caller has nothing better.
  composerDisabledTitle?: string | null;
  managedLaunchSuggestion?: ManagedLaunchSuggestion | null;
  /**
   * When true, sending while the session is locked persists as a queued
   * input that auto-dispatches at the next turn boundary. Gated by the
   * `can_queue_next_input` capability on managed-local sessions.
   */
  canQueueNextInput?: boolean;
  /**
   * When true, the managed transport supports mid-turn steer. Shows a
   * primary "Send update" action while the session is working; queue-next
   * becomes a secondary action. Turn-ended races surface as an inline
   * error with a "Queue instead" affordance.
   */
  canSteerActiveTurn?: boolean;
  /** True when backend detected stale managed execution with no active tool. */
  isStalled?: boolean;
  /** True when runtime truth says the provider is currently executing. */
  isSessionExecuting?: boolean;
  /**
   * Durable timeline rows visible in the parent workspace. When present,
   * managed-local optimistic inputs stay visible until the backend-authored
   * user row with matching input identity arrives.
   */
  timelineItems?: TimelineItem[];
}

export type SessionChatTarget = Pick<AgentSession, "id" | "project" | "provider" | "capabilities" | "session_state">;

function newClientRequestId(): string {
  const randomUUID = globalThis.crypto?.randomUUID?.bind(globalThis.crypto);
  if (randomUUID) return `web-${randomUUID()}`;
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function timelineHasDurableSubmittedInput(
  timelineItems: TimelineItem[],
  pendingInput: PendingManagedLocalInput,
): boolean {
  return timelineItems.some((item) => {
    if (item.kind !== "message") return false;
    const { event } = item;
    if (event.role !== "user" || event.is_head_branch === false) return false;
    const origin = event.input_origin;
    if (!origin || origin.authored_via !== "longhouse") return false;
    if (pendingInput.serverInputId != null && origin.session_input_id === pendingInput.serverInputId) {
      return true;
    }
    return Boolean(origin.client_request_id && origin.client_request_id === pendingInput.clientRequestId);
  });
}

export function SessionChat({
  session,
  onClose,
  emptyStateTitle,
  hintText,
  composerPlaceholder,
  layout = "panel",
  submitLabel = "Send",
  requireClickForFirstSend = false,
  keyboardHintText,
  chatMode,
  composerDisabledReason = null,
  composerDisabledTitle = null,
  managedLaunchSuggestion = null,
  canQueueNextInput = false,
  canSteerActiveTurn = false,
  isStalled = false,
  isSessionExecuting = false,
  timelineItems,
}: SessionChatProps) {
  const isDock = layout === "dock";
  const isManagedLocal = chatMode === "managed_local";
  const isComposerDisabled = Boolean(composerDisabledReason);
  const attachImagesEnabled = isManagedLocal && Boolean(session.capabilities?.attach_images);
  const composerAttachments = useComposerAttachments();
  const showComposerUnavailableState = isComposerDisabled;
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isInterrupting, setIsInterrupting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [blockedKeyboardSubmit, setBlockedKeyboardSubmit] = useState(false);

  const [sentConfirmation, setSentConfirmation] = useState(false);
  const [pendingManagedLocalInput, setPendingManagedLocalInput] =
    useState<PendingManagedLocalInput | null>(null);
  const composerTextareaRef = useRef<HTMLTextAreaElement>(null);
  const sentConfirmationTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const autoResizeDockTextarea = useCallback((el: HTMLTextAreaElement) => {
    el.style.height = "auto";
    if (el.scrollHeight > 0) {
      el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
    }
  }, []);

  useEffect(() => {
    return () => {
      if (sentConfirmationTimerRef.current) clearTimeout(sentConfirmationTimerRef.current);
    };
  }, []);

  const handleDraftChange = useCallback((nextDraft: string) => {
    setDraft(nextDraft);
    if (!nextDraft.trim()) {
      setBlockedKeyboardSubmit(false);
      if (composerTextareaRef.current) {
        composerTextareaRef.current.style.height = "auto";
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

  useEffect(() => {
    if (!pendingManagedLocalInput || !timelineItems) return;
    if (timelineHasDurableSubmittedInput(timelineItems, pendingManagedLocalInput)) {
      setPendingManagedLocalInput(null);
    }
  }, [pendingManagedLocalInput, timelineItems]);

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
          }
        : null,
    [lockStatusQuery.data],
  );
  const isSendLocked = Boolean(lockInfo?.locked) || isSessionExecuting;

  const queuedInputsQuery = useQuery<QueuedInputSummary[]>({
    queryKey: ["session-inputs", session.id],
    queryFn: async () => {
      try {
        return await fetchSessionInputs(session.id);
      } catch {
        return [];
      }
    },
    enabled: Boolean(session.id) && isManagedLocal,
    retry: false,
    refetchOnWindowFocus: false,
    // Poll while any row is queued/delivering so the UI sees drain progress.
    refetchInterval: (query) => {
      const rows = query.state.data ?? [];
      return rows.some((r) => r.status === "queued" || r.status === "delivering")
        ? 2_000
        : false;
    },
    staleTime: 10_000,
  });
  const queuedInputs = queuedInputsQuery.data ?? [];
  const activeQueuedInputs = queuedInputs.filter(
    (row) => row.status === "queued" || row.status === "delivering",
  );
  // Exclude `steer && turn_ended` rows from the failed-chip list: the user
  // already saw the actionable "Queue instead" prompt on the POST; showing a
  // duplicate red "failed" chip afterward reads like a second unrelated
  // system failure.
  const failedInputs = queuedInputs.filter(
    (row) => row.status === "failed" && !(row.intent === "steer" && row.last_error === "turn_ended"),
  );
  const queueFull = activeQueuedInputs.length >= 5;

  // Set when the most recent send failed with turn_ended; lets the UI
  // offer a one-click "Queue instead" fallback instead of silently re-mapping
  // the user's intent.
  const [turnEndedDraft, setTurnEndedDraft] = useState<string | null>(null);

  const handleManagedLocalSend = useCallback(
    async (
      message: string,
      intent: "auto" | "queue" | "steer" = "auto",
      attachments: { blob: Blob; filename: string }[] = [],
    ) => {
      const clientRequestId = newClientRequestId();
      setPendingManagedLocalInput({
        text: message,
        clientRequestId,
        serverInputId: null,
        intent,
        phase: "submitting",
      });
      setIsSubmitting(true);
      try {
        const result = attachments.length
          ? await postSessionInputMultipart(session.id, {
              text: message,
              attachments,
              client_request_id: clientRequestId,
            })
          : await postSessionInput(session.id, {
              text: message,
              intent,
              client_request_id: clientRequestId,
            });

        // Seed the queued-inputs cache immediately so the chip appears
        // before the next poll.
        queryClient.setQueryData<QueuedInputSummary[]>(
          ["session-inputs", session.id],
          result.queued,
        );

        setTurnEndedDraft(null);

        if (result.outcome === "sent") {
          queryClient.setQueryData<SessionLockInfo | null>(["session-lock", session.id], {
            locked: true,
            holder: null,
            time_remaining_seconds: null,
            fork_available: true,
          });

          if (sentConfirmationTimerRef.current) clearTimeout(sentConfirmationTimerRef.current);
          setSentConfirmation(true);
          sentConfirmationTimerRef.current = setTimeout(() => setSentConfirmation(false), 2000);

          void refreshCurrentSessionWorkspace();
          setPendingManagedLocalInput(null);
        } else {
          setPendingManagedLocalInput(null);
        }
        return true;
      } catch (e) {
        // Console turn-start failures are durable input receipts. Refresh them
        // immediately so the failed message stays attached to the turn instead
        // of being represented only by a transient request error.
        let persistedFailure = false;
        try {
          const refreshedInputs = await fetchSessionInputs(session.id);
          queryClient.setQueryData<QueuedInputSummary[]>(
            ["session-inputs", session.id],
            refreshedInputs,
          );
          persistedFailure = refreshedInputs.some(
            (row) => row.status === "failed" && row.text === message,
          );
        } catch {
          // Preserve the request error when the receipt cannot be refreshed.
        }

        // Parse structured backend errors so turn_ended on steer surfaces
        // as an actionable prompt, not a mystery failure.
        const errorBody = (e as {
          body?: { detail?: { code?: string; error_code?: string; message?: string } };
        })?.body;
        const errorCode = errorBody?.detail?.error_code ?? errorBody?.detail?.code;
        if (intent === "steer" && errorCode === "turn_ended") {
          setTurnEndedDraft(message);
          setError(errorBody?.detail?.message ?? "Active turn ended before your update arrived.");
        } else if (persistedFailure) {
          setError(null);
        } else {
          setError(e instanceof Error ? e.message : "Unknown error");
        }
        setPendingManagedLocalInput(null);
        return false;
      } finally {
        setIsSubmitting(false);
      }
    },
    [queryClient, session.id, refreshCurrentSessionWorkspace],
  );

  const handleCancelQueuedInput = useCallback(
    async (input: QueuedInputSummary) => {
      try {
        await cancelSessionInput(session.id, input);
        // Optimistically drop it from the cache; refetch to confirm.
        queryClient.setQueryData<QueuedInputSummary[]>(
          ["session-inputs", session.id],
          (rows = []) =>
            rows.filter((row) =>
              input.live_input_id
                ? row.live_input_id !== input.live_input_id
                : row.id !== input.id,
            ),
        );
        void queuedInputsQuery.refetch();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not cancel queued input");
      }
    },
    [queryClient, queuedInputsQuery, session.id],
  );

  const handleInterrupt = useCallback(async () => {
    if (!isManagedLocal || isInterrupting || session.session_state.control.actions.interrupt.state !== "available") return;
    setIsInterrupting(true);
    setError(null);
    try {
      await (session.session_state.mode === "console"
        ? interruptConsoleTurn(session.id)
        : interruptLiveSession(session.id));
      queryClient.setQueryData<SessionLockInfo | null>(["session-lock", session.id], {
        locked: false,
        holder: null,
        time_remaining_seconds: null,
        fork_available: false,
      });
      await refreshCurrentSessionWorkspace();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not interrupt running turn");
    } finally {
      setIsInterrupting(false);
    }
  }, [isInterrupting, isManagedLocal, queryClient, refreshCurrentSessionWorkspace, session.id, session.session_state]);

  const canSteerNow = isSendLocked && canSteerActiveTurn;
  const canQueueNow = isSendLocked && canQueueNextInput && !queueFull;
  const canInterruptTurn =
    isManagedLocal && session.session_state.control.actions.interrupt.state === "available";
  const attachmentInputEnabled = attachImagesEnabled && !isSendLocked;
  // Inline interrupt is only offered while a turn is actually running — an
  // idle session has nothing to stop. When the stall-recovery card is showing
  // it already exposes the same action, so we hide the composer copy to avoid
  // two buttons doing the identical thing.
  const showInlineInterrupt = canInterruptTurn && isSendLocked && !isStalled;
  // When steer is available, the primary action is steer. Queue-next becomes
  // a secondary escape hatch. If only queue is available, primary = queue.
  const primaryIntent: "auto" | "queue" | "steer" = !isSendLocked
    ? "auto"
    : canSteerNow
    ? "steer"
    : canQueueNow
    ? "queue"
    : "auto";
  // Primary send is blocked when there's no available action.
  const isSendBlocked = isSendLocked && !canSteerNow && !canQueueNow;
  const attachmentSendBlocked =
    composerAttachments.attachments.length > 0 && primaryIntent !== "auto";
  // Attachment-only sends are valid when the route accepts them.
  const hasComposerContent = Boolean(draft.trim()) || composerAttachments.attachments.length > 0;

  const handleSend = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();

      const message = draft.trim();
      const pendingAttachments = composerAttachments.attachments;
      const hasAttachments = pendingAttachments.length > 0;
      // Attachments require message text only when the user wrote nothing —
      // attachment-only sends are valid and the server accepts text="".
      if (!message && !hasAttachments) return;
      if (isSubmitting || isComposerDisabled || isSendBlocked) return;
      if (hasAttachments && primaryIntent !== "auto") {
        setError("Image attachments can only be sent when the session is ready for a new turn.");
        return;
      }
      // Block send while compression is in flight; the snapshot would miss
      // the pending file and the late add could repopulate the cleared tray.
      if (composerAttachments.isCompressing) return;

      setDraft("");
      setError(null);
      setBlockedKeyboardSubmit(false);

      const attachmentArgs = hasAttachments
        ? pendingAttachments.map((a) => ({ blob: a.blob, filename: a.filename }))
        : [];
      const sent = await handleManagedLocalSend(message, primaryIntent, attachmentArgs);
      if (sent) {
        if (hasAttachments) composerAttachments.clear();
      } else {
        setDraft(message);
      }
    },
    [
      draft,
      isSubmitting,
      handleManagedLocalSend,
      isComposerDisabled,
      isSendBlocked,
      primaryIntent,
      composerAttachments,
    ],
  );

  const handleComposerPaste = useCallback(
    (e: React.ClipboardEvent) => {
      if (!attachmentInputEnabled) return;
      const files: File[] = [];
      for (const item of Array.from(e.clipboardData.items)) {
        if (item.kind === "file" && item.type.startsWith("image/")) {
          const f = item.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length) {
        e.preventDefault();
        void composerAttachments.addFiles(files);
      }
    },
    [attachmentInputEnabled, composerAttachments],
  );

  const handleComposerDrop = useCallback(
    (e: React.DragEvent) => {
      if (!attachmentInputEnabled) return;
      const dropped = Array.from(e.dataTransfer.files);
      if (!dropped.length) return;
      // Always preventDefault on file drops — even non-image drops, otherwise
      // the browser navigates away and the user loses their draft.
      e.preventDefault();
      const images = dropped.filter((f) => f.type.startsWith("image/"));
      if (images.length) void composerAttachments.addFiles(images);
    },
    [attachmentInputEnabled, composerAttachments],
  );

  const handleComposerDragOver = useCallback(
    (e: React.DragEvent) => {
      if (!attachmentInputEnabled) return;
      if (Array.from(e.dataTransfer.items).some((it) => it.kind === "file")) {
        e.preventDefault();
      }
    },
    [attachmentInputEnabled],
  );

  const handleSecondaryQueue = useCallback(async () => {
    const message = draft.trim();
    if (!message || isSubmitting || !canQueueNow) return;
    setDraft("");
    setError(null);
    await handleManagedLocalSend(message, "queue");
  }, [draft, isSubmitting, canQueueNow, handleManagedLocalSend]);

  const handleQueueInsteadAfterTurnEnded = useCallback(async () => {
    if (!turnEndedDraft) return;
    setError(null);
    const text = turnEndedDraft;
    setTurnEndedDraft(null);
    await handleManagedLocalSend(text, "queue");
  }, [turnEndedDraft, handleManagedLocalSend]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      // Don't silently queue via Enter while working — require an explicit
      // click when the outcome would be "queued" so the user sees it.
      if (isSendLocked) {
        return;
      }
      if (requireClickForFirstSend && draft.trim() && !blockedKeyboardSubmit) {
        setBlockedKeyboardSubmit(true);
        return;
      }
      handleSend(e as unknown as FormEvent);
    }
  };

  const statusBadge = isComposerDisabled
    ? { variant: "warning" as const, label: "Unavailable" }
    : isSubmitting
      ? { variant: "warning" as const, label: "Sending" }
    : isStalled
      ? { variant: "warning" as const, label: "Stalled" }
    : isSendLocked
      ? { variant: "warning" as const, label: "Working" }
      : { variant: "neutral" as const, label: "Input available" };
  const submitButtonLabel = !isSendLocked
    ? submitLabel
    : canSteerNow
    ? "Send update"
    : canQueueNow
    ? "Queue next"
    : queueFull
    ? "Queue full"
    : "Waiting";
  let turnNoticeText =
    "Agent is working. You can draft the next message; sending will be available when it is ready.";
  if (canSteerNow) {
    if (canQueueNow && canInterruptTurn) {
      turnNoticeText =
        "Agent is working. Send update injects mid-turn, Queue next waits, Stop interrupts - Enter will not send while working.";
    } else if (canInterruptTurn) {
      turnNoticeText =
        "Agent is working. Send update injects mid-turn, Stop interrupts - Enter will not send while working.";
    } else if (canQueueNow) {
      turnNoticeText =
        "Agent is working. Send update injects mid-turn, or Queue next waits - Enter will not send while working.";
    } else {
      turnNoticeText =
        "Agent is working. Send update injects mid-turn - Enter will not send while working.";
    }
  } else if (canQueueNow) {
    turnNoticeText = canInterruptTurn
      ? "Agent is working. Queue next auto-sends at the next turn boundary, Stop interrupts - Enter will not queue."
      : "Agent is working. Queue next auto-sends at the next turn boundary - Enter will not queue.";
  } else if (canQueueNextInput && queueFull) {
    turnNoticeText = canInterruptTurn
      ? "Agent is working. The queue is full, but Stop can interrupt the current turn."
      : "Agent is working. The queue is full; sending will be available when space opens.";
  } else if (canInterruptTurn) {
    turnNoticeText =
      "Agent is working. Draft a message or Stop to interrupt; sending will be available when it is ready.";
  }

  return (
    <div
      className={`session-chat${isDock ? " session-chat--dock" : ""}`}
      data-testid={isDock ? "session-continuation-panel" : undefined}
    >
      {isDock ? null : (
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
              {session.project ? (
                <span className="session-chat-title">{session.project}</span>
              ) : null}
              <span className="session-chat-provider">
                <ProviderGlyph provider={session.provider} size={16} />
                {getProviderLabel(session.provider)}
              </span>
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

      {isManagedLocal && isStalled ? (
        <div className="session-chat-stall-recovery" data-testid="session-chat-stall-recovery">
          <div className="session-chat-stall-recovery__copy">
            <span className="session-chat-stall-recovery__title">Managed session appears stalled</span>
            <span className="session-chat-stall-recovery__detail">
              No progress has arrived from this managed session. Interrupt releases the current turn.
            </span>
          </div>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={() => void handleInterrupt()}
            disabled={isInterrupting}
          >
            {isInterrupting ? "Interrupting" : "Interrupt"}
          </Button>
        </div>
      ) : null}

      {isSendLocked && !isStalled && (
        <div className="session-chat-turn-notice">
          <span>{turnNoticeText}</span>
        </div>
      )}

      {turnEndedDraft ? (
        <div
          className="session-chat-queued session-chat-queued--failed"
          data-testid="session-chat-turn-ended"
        >
          <div className="session-chat-queued__label">Active turn ended</div>
          <div className="session-chat-queued__item">
            <span className="session-chat-queued__text">{turnEndedDraft}</span>
            <button
              type="button"
              className="session-chat-queued__cancel"
              onClick={() => void handleQueueInsteadAfterTurnEnded()}
            >
              Queue instead
            </button>
            <button
              type="button"
              className="session-chat-queued__cancel"
              onClick={() => setTurnEndedDraft(null)}
            >
              Dismiss
            </button>
          </div>
        </div>
      ) : null}

      {isManagedLocal && activeQueuedInputs.length > 0 ? (
        <div className="session-chat-queued" data-testid="session-chat-queued">
          <div className="session-chat-queued__label">Queued (auto-sends next)</div>
          <ul className="session-chat-queued__list">
            {activeQueuedInputs.map((row) => (
              <li key={row.live_input_id ?? row.id ?? row.text} className="session-chat-queued__item">
                <span className="session-chat-queued__text">{row.text}</span>
                <span
                  className={`session-chat-queued__status session-chat-queued__status--${row.status}`}
                >
                  {row.status === "delivering" ? row.last_error || "Sending…" : "Queued"}
                </span>
                {row.status === "queued" ? (
                  <button
                    type="button"
                    className="session-chat-queued__cancel"
                    onClick={() => void handleCancelQueuedInput(row)}
                    aria-label="Cancel queued message"
                  >
                    Cancel
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {isManagedLocal && failedInputs.length > 0 ? (
        <div
          className="session-chat-queued session-chat-queued--failed"
          data-testid="session-chat-queued-failed"
        >
          <div className="session-chat-queued__label">Delivery failed</div>
          <ul className="session-chat-queued__list">
            {failedInputs.map((row) => (
              <li key={row.live_input_id ?? row.id ?? row.text} className="session-chat-queued__item">
                <span className="session-chat-queued__text">{row.text}</span>
                <span className="session-chat-queued__status session-chat-queued__status--failed">
                  {row.last_error || "failed"}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {isDock ? null : (
        <div className="session-chat-messages">
          <div className="session-chat-empty">
            <p>{emptyStateTitle || "Start a conversation with this session."}</p>
            <p className="session-chat-hint">
              {hintText
                || (isManagedLocal
                  ? `Longhouse will send your next prompt into the live ${session.provider} session.`
                  : "Earlier synced turns stay visible here. Your first message continues from that context.")}
            </p>
          </div>
        </div>
      )}

      <form
        className={`session-chat-composer${isDock ? " session-chat-composer--dock" : ""}`}
        onSubmit={handleSend}
        title={composerDisabledReason ?? undefined}
        onPaste={attachmentInputEnabled ? handleComposerPaste : undefined}
        onDrop={attachmentInputEnabled ? handleComposerDrop : undefined}
        onDragOver={attachmentInputEnabled ? handleComposerDragOver : undefined}
      >
        {showComposerUnavailableState ? (
          managedLaunchSuggestion ? (
            <ManagedLaunchHintCard
              suggestion={managedLaunchSuggestion}
              testId="session-chat-managed-launch-hint"
            />
          ) : (
            <div
              className="session-chat-composer-unavailable"
              data-testid="session-chat-disabled-reason"
            >
              <span className="session-chat-composer-unavailable__title">
                {composerDisabledTitle || "Control unavailable"}
              </span>
              <span className="session-chat-composer-unavailable__copy">
                {composerDisabledReason}
              </span>
            </div>
          )
        ) : (
          <>
            {blockedKeyboardSubmit ? (
              <div className="session-chat-confirmation" data-testid="session-chat-explicit-submit-hint">
                {keyboardHintText || `Click "${submitLabel}" to confirm.`}
              </div>
            ) : isDock ? null : (
              <div className="session-chat-confirmation session-chat-confirmation--spacer" aria-hidden="true" />
            )}
            {isManagedLocal && pendingManagedLocalInput ? (
              <div className="session-chat-pending-message">
                <span className="session-chat-pending-message__text">{pendingManagedLocalInput.text}</span>
                <span className="session-chat-pending-message__status">
                  Delivering...
                </span>
                <span
                  className="session-chat-pending-message__spinner"
                  aria-label="Sending"
                />
              </div>
            ) : null}
            {attachImagesEnabled ? (
              <AttachmentTray
                attachments={composerAttachments.attachments}
                onAddFiles={composerAttachments.addFiles}
                onRemove={composerAttachments.removeAttachment}
                isCompressing={composerAttachments.isCompressing}
                error={composerAttachments.error}
                onClearError={composerAttachments.clearError}
                disabled={isSubmitting}
                addDisabled={!attachmentInputEnabled}
              />
            ) : null}
            {isDock ? (
              <div className="session-chat-composer-row">
                <textarea
                  ref={composerTextareaRef}
                  value={draft}
                  onChange={(e) => {
                    handleDraftChange(e.target.value);
                    autoResizeDockTextarea(e.target);
                  }}
                  onKeyDown={handleKeyDown}
                  placeholder={composerPlaceholder || "Message"}
                  disabled={isSubmitting}
                  rows={1}
                />
                {isManagedLocal && sentConfirmation ? (
                  <span className="session-chat-sent-notice">Sent</span>
                ) : null}
                {showInlineInterrupt ? (
                  <Button
                    type="button"
                    variant="danger"
                    size="sm"
                    onClick={() => void handleInterrupt()}
                    disabled={isInterrupting}
                    data-testid="session-chat-interrupt"
                  >
                    {isInterrupting ? "Stopping" : "Stop"}
                  </Button>
                ) : null}
                {canSteerNow && canQueueNow ? (
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    onClick={() => void handleSecondaryQueue()}
                    disabled={!draft.trim() || isSubmitting}
                  >
                    Queue next
                  </Button>
                ) : null}
                <Button
                  type="submit"
                  variant="primary"
                  size="sm"
                  disabled={!hasComposerContent || isSubmitting || isSendBlocked || attachmentSendBlocked || composerAttachments.isCompressing}
                >
                  {submitButtonLabel}
                </Button>
              </div>
            ) : (
              <>
                <textarea
                  ref={composerTextareaRef}
                  value={draft}
                  onChange={(e) => handleDraftChange(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder={composerPlaceholder || "Message"}
                  disabled={isComposerDisabled || isSubmitting}
                  rows={2}
                  title={composerDisabledReason ?? undefined}
                />
                <div className="session-chat-actions">
                  {showInlineInterrupt ? (
                    <Button
                      type="button"
                      variant="danger"
                      size="sm"
                      onClick={() => void handleInterrupt()}
                      disabled={isInterrupting}
                      data-testid="session-chat-interrupt"
                    >
                      {isInterrupting ? "Stopping" : "Stop"}
                    </Button>
                  ) : null}
                  {canSteerNow && canQueueNow ? (
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      onClick={() => void handleSecondaryQueue()}
                      disabled={isComposerDisabled || !draft.trim() || isSubmitting}
                    >
                      Queue next
                    </Button>
                  ) : null}
                  <Button
                    type="submit"
                    variant="primary"
                    size="sm"
                    disabled={isComposerDisabled || !hasComposerContent || isSubmitting || isSendBlocked || attachmentSendBlocked || composerAttachments.isCompressing}
                    title={composerDisabledReason ?? undefined}
                  >
                    {submitButtonLabel}
                  </Button>
                </div>
              </>
            )}
          </>
        )}
      </form>
    </div>
  );
}
