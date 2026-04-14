import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Navigate, useParams } from "react-router-dom";
import toast from "react-hot-toast";
import { PresenceBadge, PresenceHero } from "../components/PresenceBadge";
import { SidebarIcon, XIcon } from "../components/icons";
import {
  Badge,
  Button,
  EmptyState,
  PageShell,
  Spinner,
} from "../components/ui";
import { useLoopInstallPrompt } from "../hooks/useLoopInstallPrompt";
import { usePageMeta } from "../hooks/usePageMeta";
import { normalizeExecutionVenueLabel } from "../lib/sessionExecutionHome";
import {
  connectTimelineSessionsStream,
  fetchAgentSession,
  fetchAgentSessionPreview,
  fetchAgentSessions,
  setSessionAction,
  type AgentSession,
  type AgentSessionPreview,
  type TimelineSessionCard,
} from "../services/api/agents";
import { sendLiveSessionMessage } from "../services/api/sessionChat";
import "../styles/loop-inbox.css";

const LOOP_SESSION_FILTERS = {
  days_back: 14,
  limit: 100,
} as const;

const PHONE_BREAKPOINT_PX = 768;

type AttentionState = "blocked" | "needs_user";

interface LoopQueueItem {
  session: AgentSession;
  title: string;
  summary: string;
  machine: string | null;
  lastActivityAt: string;
  venueLabel: string | null;
  attentionState: AttentionState;
}

type BadgeNavigator = Navigator & {
  setAppBadge?: (contents?: number) => Promise<void>;
  clearAppBadge?: () => Promise<void>;
};

function isAttentionState(
  state: string | null | undefined,
): state is AttentionState {
  return state === "blocked" || state === "needs_user";
}

function isActionableSession(
  session: AgentSession | null | undefined,
): session is AgentSession {
  return Boolean(
    session &&
    session.user_state === "active" &&
    isAttentionState(session.presence_state),
  );
}

function isUuidLike(value: string | undefined): value is string {
  if (!value) return false;
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
    value,
  );
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "Just now";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "Just now";
  return parsed.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function attentionBadgeVariant(
  state: AttentionState | null | undefined,
): "warning" | "error" {
  return state === "blocked" ? "error" : "warning";
}

function formatAttentionLabel(
  state: AttentionState | null | undefined,
): string {
  if (state === "blocked") return "Needs permission";
  return "Waiting on you";
}

function formatQueueCount(count: number): string {
  return `${count} session${count === 1 ? "" : "s"} waiting`;
}

function getLoopTitle(session: AgentSession): string {
  const summaryTitle = session.summary_title?.trim();
  if (summaryTitle) return summaryTitle;
  const summary = session.summary?.trim();
  if (summary)
    return summary.length > 72 ? `${summary.slice(0, 72).trim()}...` : summary;
  const firstUser = session.first_user_message?.trim();
  if (firstUser)
    return firstUser.length > 72
      ? `${firstUser.slice(0, 72).trim()}...`
      : firstUser;
  return `${session.provider} session`;
}

function getLoopSummary(session: AgentSession): string {
  const summary = session.summary?.trim();
  if (summary) return summary;
  if (session.presence_state === "blocked") {
    return session.presence_tool
      ? `This session is blocked while waiting on ${session.presence_tool}.`
      : "This session is blocked and needs a decision before it can continue.";
  }
  return "This session is waiting for your next instruction.";
}

function getMachineLabel(session: AgentSession): string | null {
  return (
    session.origin_label || session.device_id || session.environment || null
  );
}

function buildLoopQueue(cards: TimelineSessionCard[]): LoopQueueItem[] {
  const actionableSessions = cards
    .map((card) => card.head)
    .filter(
      (
        session,
      ): session is AgentSession & {
        presence_state: AttentionState;
        user_state: "active";
      } => isActionableSession(session),
    );

  return actionableSessions
    .map((session) => ({
      session,
      title: getLoopTitle(session),
      summary: getLoopSummary(session),
      machine: getMachineLabel(session),
      lastActivityAt:
        session.timeline_anchor_at ||
        session.last_activity_at ||
        session.started_at,
      venueLabel: normalizeExecutionVenueLabel(session.home_label),
      attentionState: session.presence_state,
    }))
    .sort((left, right) => {
      const leftPriority = left.attentionState === "blocked" ? 0 : 1;
      const rightPriority = right.attentionState === "blocked" ? 0 : 1;
      if (leftPriority !== rightPriority) return leftPriority - rightPriority;
      return (
        new Date(right.lastActivityAt).getTime() -
        new Date(left.lastActivityAt).getTime()
      );
    });
}

function getPreviewText(
  preview: AgentSessionPreview | undefined,
  role: "user" | "assistant",
): string | null {
  if (!preview) return null;
  for (let index = preview.messages.length - 1; index >= 0; index -= 1) {
    const message = preview.messages[index];
    if (message.role === role && message.content.trim()) {
      return message.content.trim();
    }
  }
  return null;
}

function statusHeadline(session: AgentSession | null): string {
  if (!session) return "No session selected";
  if (session.user_state === "snoozed") return "This session is snoozed";
  if (session.user_state === "parked") return "This session is parked";
  if (session.user_state === "archived") return "This session is archived";
  if (session.presence_state === "blocked")
    return "This session needs permission";
  if (session.presence_state === "needs_user")
    return "This session is waiting on you";
  return "This session is no longer waiting on you";
}

function statusDescription(session: AgentSession | null): string {
  if (!session)
    return "Open the current queue to review the next session that actually needs attention.";
  if (session.user_state === "snoozed") {
    return "You already deferred this session. It will stay out of the phone queue until it becomes active again.";
  }
  if (session.user_state === "parked") {
    return "This session was intentionally parked, so it is not part of the active phone queue.";
  }
  if (session.user_state === "archived") {
    return "This session was archived and no longer belongs in the active phone queue.";
  }
  return "The live session state moved on. Open the current queue for the next session that still needs a response.";
}

function useIsPhoneLayout(): boolean {
  const [isPhoneLayout, setIsPhoneLayout] = useState(() => {
    if (typeof window === "undefined") return false;
    return window.innerWidth < PHONE_BREAKPOINT_PX;
  });

  useEffect(() => {
    if (typeof window === "undefined") return undefined;

    const updateLayout = () => {
      setIsPhoneLayout(window.innerWidth < PHONE_BREAKPOINT_PX);
    };

    updateLayout();
    window.addEventListener("resize", updateLayout);
    return () => window.removeEventListener("resize", updateLayout);
  }, []);

  return isPhoneLayout;
}

function LoopSessionRow({
  item,
  selected,
  to,
  compact = false,
  onSelect,
}: {
  item: LoopQueueItem;
  selected: boolean;
  to: string;
  compact?: boolean;
  onSelect?: () => void;
}) {
  return (
    <Link
      to={to}
      className={`loop-inbox-row loop-inbox-row--${item.attentionState.replace(/_/g, "-")}${selected ? " is-selected" : ""}${
        compact ? " loop-inbox-row--compact" : ""
      }`}
      onClick={onSelect}
      aria-current={selected ? "page" : undefined}
      data-testid={`loop-inbox-row-${item.session.id}`}
    >
      <div className="loop-inbox-row-top">
        <strong>{item.title}</strong>
        <div className="loop-inbox-row-badges">
          <Badge variant={attentionBadgeVariant(item.attentionState)}>
            {formatAttentionLabel(item.attentionState)}
          </Badge>
        </div>
      </div>
      <div className="loop-inbox-row-meta">
        {item.venueLabel && (
          <span className="loop-inbox-home-label">{item.venueLabel}</span>
        )}
        {item.session.project && <span>{item.session.project}</span>}
        {item.machine && <span>{item.machine}</span>}
        <span>{formatTimestamp(item.lastActivityAt)}</span>
      </div>
      <p>{item.summary}</p>
    </Link>
  );
}

export default function LoopInboxPage() {
  const { sessionId: sessionIdParam } = useParams<{ sessionId?: string }>();
  const queryClient = useQueryClient();
  const { canInstall, showIosHint, isInstalled, install } =
    useLoopInstallPrompt();
  const isPhoneLayout = useIsPhoneLayout();
  const [queueOpen, setQueueOpen] = useState(false);
  const [replyText, setReplyText] = useState("");

  usePageMeta({
    title: "Loop Inbox | Longhouse",
    description:
      "Open Longhouse from your phone and handle the sessions waiting on you.",
  });

  const selectedSessionId = isUuidLike(sessionIdParam) ? sessionIdParam : null;
  const hasInvalidSelection = Boolean(sessionIdParam && !selectedSessionId);

  const queueQuery = useQuery({
    queryKey: ["loop-session-list"],
    queryFn: () => fetchAgentSessions(LOOP_SESSION_FILTERS),
    staleTime: 10_000,
    refetchInterval: typeof EventSource === "undefined" ? 15_000 : false,
  });

  const queueItems = useMemo(
    () => buildLoopQueue(queueQuery.data?.sessions ?? []),
    [queueQuery.data],
  );

  const selectedQueueItem =
    queueItems.find((item) => item.session.id === selectedSessionId) ?? null;

  const sessionQuery = useQuery({
    queryKey: ["loop-selected-session", selectedSessionId],
    queryFn: () => fetchAgentSession(selectedSessionId as string),
    enabled: Boolean(selectedSessionId),
    staleTime: 10_000,
  });

  const previewQuery = useQuery({
    queryKey: ["loop-selected-preview", selectedSessionId],
    queryFn: () => fetchAgentSessionPreview(selectedSessionId as string, 8),
    enabled: Boolean(selectedSessionId),
    staleTime: 10_000,
  });

  useEffect(() => {
    if (typeof EventSource === "undefined") return undefined;

    return connectTimelineSessionsStream(
      LOOP_SESSION_FILTERS,
      {
        onSessionUpsert: () => {
          void queryClient.invalidateQueries({
            queryKey: ["loop-session-list"],
          });
        },
        onSessionRemove: () => {
          void queryClient.invalidateQueries({
            queryKey: ["loop-session-list"],
          });
        },
      },
      { skipInitialReplay: true },
    );
  }, [queryClient]);

  useEffect(() => {
    if (!isPhoneLayout) {
      setQueueOpen(false);
    }
  }, [isPhoneLayout]);

  useEffect(() => {
    if (!(isPhoneLayout && queueOpen)) return undefined;

    const previousOverflow = document.body.style.overflow;
    const closeQueue = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setQueueOpen(false);
      }
    };

    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", closeQueue);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", closeQueue);
    };
  }, [isPhoneLayout, queueOpen]);

  useEffect(() => {
    const badgeNavigator = navigator as BadgeNavigator;
    if (
      typeof badgeNavigator.setAppBadge !== "function" &&
      typeof badgeNavigator.clearAppBadge !== "function"
    ) {
      return;
    }

    const count = queueItems.length;
    if (count > 0 && typeof badgeNavigator.setAppBadge === "function") {
      void badgeNavigator.setAppBadge(count).catch(() => {});
      return;
    }

    if (count === 0 && typeof badgeNavigator.clearAppBadge === "function") {
      void badgeNavigator.clearAppBadge().catch(() => {});
    }
  }, [queueItems.length]);

  const currentSession =
    sessionQuery.data ?? selectedQueueItem?.session ?? null;
  const currentPreview = previewQuery.data;
  const lastUserText = getPreviewText(currentPreview, "user");
  const lastAssistantText = getPreviewText(currentPreview, "assistant");
  const currentVenueLabel = normalizeExecutionVenueLabel(
    currentSession?.home_label,
  );
  const currentMachineLabel = currentSession
    ? getMachineLabel(currentSession)
    : null;
  const currentPresenceState = currentSession?.presence_state ?? null;
  const currentPresenceTool = currentSession?.presence_tool ?? null;
  const currentSummary = currentSession ? getLoopSummary(currentSession) : null;
  const currentTitle = currentSession
    ? getLoopTitle(currentSession)
    : (selectedQueueItem?.title ?? null);
  const currentStillActionable = isActionableSession(currentSession);
  const currentAttentionState: AttentionState | null =
    currentStillActionable &&
    currentSession &&
    isAttentionState(currentSession.presence_state)
      ? currentSession.presence_state
      : null;
  const currentRecoverySessionId = queueItems[0]?.session.id ?? null;
  const showInstallBanner = !isInstalled && (canInstall || showIosHint);
  const queueSummaryLabel =
    queueItems.length === 0
      ? "No sessions waiting"
      : formatQueueCount(queueItems.length);
  const showMobileTopBar = isPhoneLayout;

  const snoozeMutation = useMutation({
    mutationFn: async (sessionId: string) =>
      setSessionAction(sessionId, "snooze"),
    onSuccess: async () => {
      setReplyText("");
      toast.success("Session snoozed.");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["loop-session-list"] }),
        queryClient.invalidateQueries({
          queryKey: ["loop-selected-session", selectedSessionId],
        }),
        queryClient.invalidateQueries({
          queryKey: ["loop-selected-preview", selectedSessionId],
        }),
      ]);
    },
    onError: (error) => {
      toast.error(
        error instanceof Error ? error.message : "Unable to snooze session.",
      );
    },
  });

  const resumeMutation = useMutation({
    mutationFn: async (sessionId: string) =>
      setSessionAction(sessionId, "resume"),
    onSuccess: async () => {
      toast.success("Session moved back into the active queue.");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["loop-session-list"] }),
        queryClient.invalidateQueries({
          queryKey: ["loop-selected-session", selectedSessionId],
        }),
      ]);
    },
    onError: (error) => {
      toast.error(
        error instanceof Error ? error.message : "Unable to resume session.",
      );
    },
  });

  const replyMutation = useMutation({
    mutationFn: async ({
      sessionId,
      message,
    }: {
      sessionId: string;
      message: string;
    }) => {
      const result = await sendLiveSessionMessage(sessionId, message);
      if (!result.accepted) {
        throw new Error(result.error || "Session did not accept the message.");
      }
      return result;
    },
    onSuccess: async () => {
      setReplyText("");
      toast.success("Reply sent to the live session.");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["loop-session-list"] }),
        queryClient.invalidateQueries({
          queryKey: ["loop-selected-session", selectedSessionId],
        }),
        queryClient.invalidateQueries({
          queryKey: ["loop-selected-preview", selectedSessionId],
        }),
      ]);
    },
    onError: (error) => {
      toast.error(
        error instanceof Error
          ? error.message
          : "Unable to reply to the live session.",
      );
    },
  });

  if (!selectedSessionId && queueItems[0]) {
    return <Navigate to={`/loop/${queueItems[0].session.id}`} replace />;
  }

  if (hasInvalidSelection && queueItems[0]) {
    return <Navigate to={`/loop/${queueItems[0].session.id}`} replace />;
  }

  const queueButtonAriaLabel =
    queueItems.length > 0
      ? `Open the follow-up queue. ${queueSummaryLabel}.`
      : "Open the follow-up queue.";

  const installBanner = showInstallBanner ? (
    <section
      className={`loop-install-banner${isPhoneLayout ? " loop-install-banner--compact" : ""}`}
      data-testid="loop-install-banner"
    >
      <div>
        <strong>Install Loop</strong>
        <p>
          {isPhoneLayout
            ? "Save this phone view to your home screen."
            : "Save this view to your iPhone home screen so sessions waiting on you reopen instantly."}
        </p>
      </div>
      <div className="loop-install-banner-actions">
        {canInstall && (
          <Button
            onClick={() => void install()}
            data-testid="loop-install-action"
          >
            Install app
          </Button>
        )}
        {showIosHint && (
          <p className="loop-install-hint">
            On iPhone: tap Share, then <strong>Add to Home Screen.</strong>
          </p>
        )}
      </div>
    </section>
  ) : null;

  const mobileQueueDrawer = showMobileTopBar ? (
    <>
      <div
        className={`loop-inbox-queue-drawer-scrim${queueOpen ? " is-open" : ""}`}
        data-testid="loop-mobile-queue-scrim"
        onClick={() => setQueueOpen(false)}
        aria-hidden="true"
      />
      <aside
        id="loop-mobile-queue-drawer"
        className={`loop-inbox-queue-drawer${queueOpen ? " is-open" : ""}`}
        role="dialog"
        aria-modal={queueOpen ? true : undefined}
        aria-hidden={!queueOpen}
        aria-labelledby="loop-mobile-queue-drawer-title"
        data-testid="loop-mobile-queue-drawer"
      >
        <div className="loop-inbox-queue-drawer-header">
          <div>
            <div className="loop-inbox-list-label">Loop Inbox</div>
            <h2
              id="loop-mobile-queue-drawer-title"
              className="loop-inbox-queue-drawer-title"
            >
              Sessions waiting
            </h2>
            <p className="loop-inbox-queue-drawer-summary">
              {queueSummaryLabel}
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="loop-inbox-queue-drawer-close"
            onClick={() => setQueueOpen(false)}
            aria-label="Close follow-up queue"
            data-testid="loop-mobile-queue-close"
          >
            <XIcon width={18} height={18} />
          </Button>
        </div>

        <div className="loop-inbox-queue-drawer-body">
          {queueQuery.isLoading && queueItems.length === 0 ? (
            <div className="loop-inbox-card-loading loop-inbox-queue-drawer-loading">
              <Spinner size="sm" />
              <span>Loading waiting sessions…</span>
            </div>
          ) : queueItems.length > 0 ? (
            queueItems.map((item) => (
              <LoopSessionRow
                key={item.session.id}
                item={item}
                selected={item.session.id === selectedSessionId}
                to={`/loop/${item.session.id}`}
                compact
                onSelect={() => setQueueOpen(false)}
              />
            ))
          ) : (
            <div
              className="loop-inbox-queue-drawer-empty"
              data-testid="loop-mobile-queue-drawer-empty"
            >
              <strong>No sessions waiting right now</strong>
              <p>
                When a live session blocks or asks for input, it will show up
                here.
              </p>
            </div>
          )}
        </div>

        <div className="loop-inbox-queue-drawer-footer">
          <Link
            className="ui-button ui-button--ghost ui-button--md loop-inbox-queue-drawer-footer-link"
            to="/timeline"
            onClick={() => setQueueOpen(false)}
          >
            Open timeline
          </Link>
        </div>
      </aside>
    </>
  ) : null;

  const showStaleBanner = Boolean(currentSession && !currentStillActionable);
  const canReplyLive = Boolean(
    currentSession?.capabilities?.reply_to_live_session_available,
  );
  const canResumeSession = Boolean(
    currentSession &&
    currentSession.user_state &&
    currentSession.user_state !== "active",
  );
  const isLoadingCard = sessionQuery.isLoading || previewQuery.isLoading;

  return (
    <PageShell size="wide" className="loop-inbox-shell">
      <div className="loop-inbox-page">
        {showMobileTopBar && (
          <header
            className="loop-inbox-mobile-header"
            data-testid="loop-mobile-header"
          >
            <div className="loop-inbox-mobile-header-slot">
              <button
                type="button"
                className={`loop-inbox-mobile-header-trigger${queueOpen ? " is-active" : ""}`}
                onClick={() => setQueueOpen((open) => !open)}
                aria-haspopup="dialog"
                aria-controls="loop-mobile-queue-drawer"
                aria-expanded={queueOpen}
                aria-label={queueButtonAriaLabel}
                data-testid="loop-mobile-queue-button"
              >
                <SidebarIcon
                  width={18}
                  height={18}
                  className="loop-inbox-mobile-header-trigger-icon"
                />
                <span className="loop-inbox-mobile-header-trigger-label">
                  Queue
                </span>
                <span
                  className={`loop-inbox-mobile-header-trigger-count${queueItems.length === 0 ? " is-empty" : ""}`}
                  data-testid="loop-mobile-queue-count"
                  aria-hidden="true"
                >
                  {queueItems.length}
                </span>
              </button>
            </div>
            <div className="loop-inbox-mobile-header-copy">
              <div className="loop-inbox-mobile-header-title">
                Longhouse Loop
              </div>
            </div>
          </header>
        )}

        {!isPhoneLayout && (
          <header className="loop-inbox-header">
            <div className="loop-inbox-header-copy">
              <span className="loop-inbox-eyebrow">iPhone home screen</span>
              <h1>Loop Inbox</h1>
              <p>
                Open the sessions waiting on you, send a quick reply when the
                live session supports it, or defer the rest until you are back
                at a real workstation.
              </p>
            </div>
            <Link
              className="ui-button ui-button--ghost ui-button--md"
              to="/timeline"
            >
              Open timeline
            </Link>
          </header>
        )}

        {!isPhoneLayout && installBanner}

        {queueQuery.isLoading && !currentSession && (
          <div className="loop-inbox-loading">
            <Spinner size="md" />
            <span>Loading Loop inbox…</span>
          </div>
        )}

        {!queueQuery.isLoading &&
          queueItems.length === 0 &&
          !currentSession && (
            <EmptyState
              title="No sessions need attention"
              description="When a live session blocks or waits for input, it will show up here."
              action={
                <Link
                  className="ui-button ui-button--secondary ui-button--md"
                  to="/timeline"
                >
                  Open timeline
                </Link>
              }
            />
          )}

        {(queueItems.length > 0 || currentSession || isLoadingCard) &&
          (isPhoneLayout ? (
            <>
              <section
                className="loop-inbox-card"
                data-testid="loop-inbox-card"
              >
                {showStaleBanner && (
                  <div
                    className="loop-inbox-card-status-banner"
                    data-testid="loop-inbox-card-status-banner"
                  >
                    <div className="loop-inbox-card-status-banner-copy">
                      <div className="loop-inbox-list-label">
                        Current status
                      </div>
                      <h3>{statusHeadline(currentSession)}</h3>
                      <p>{statusDescription(currentSession)}</p>
                    </div>
                    {currentRecoverySessionId &&
                      currentRecoverySessionId !== selectedSessionId && (
                        <Link
                          className="ui-button ui-button--secondary ui-button--sm"
                          to={`/loop/${currentRecoverySessionId}`}
                        >
                          Open current
                        </Link>
                      )}
                  </div>
                )}

                {isLoadingCard && !currentSession ? (
                  <div className="loop-inbox-card-loading">
                    <Spinner size="sm" />
                    <span>Loading session…</span>
                  </div>
                ) : currentSession ? (
                  <>
                    <div className="loop-inbox-card-top">
                      <div>
                        <h2>{currentTitle}</h2>
                        <div className="loop-inbox-card-meta">
                          {currentVenueLabel && (
                            <span className="loop-inbox-home-label">
                              {currentVenueLabel}
                            </span>
                          )}
                          {currentSession.project && (
                            <span>{currentSession.project}</span>
                          )}
                          {currentMachineLabel && (
                            <span>{currentMachineLabel}</span>
                          )}
                          <span>
                            {formatTimestamp(
                              currentSession.timeline_anchor_at ||
                                currentSession.last_activity_at ||
                                currentSession.started_at,
                            )}
                          </span>
                        </div>
                      </div>
                      {currentStillActionable && (
                        <div className="loop-inbox-card-top-badges">
                          <Badge
                            variant={attentionBadgeVariant(
                              currentAttentionState,
                            )}
                          >
                            {formatAttentionLabel(currentAttentionState)}
                          </Badge>
                        </div>
                      )}
                    </div>

                    <PresenceHero
                      state={currentPresenceState}
                      tool={currentPresenceTool}
                    />

                    <div className="loop-inbox-card-section">
                      <h3>What is happening</h3>
                      <p>{currentSummary}</p>
                    </div>

                    <div className="loop-inbox-card-context">
                      <div className="loop-inbox-card-section">
                        <h3>Last user instruction</h3>
                        <p>
                          {lastUserText ||
                            "No recent user message available yet."}
                        </p>
                      </div>
                      <div className="loop-inbox-card-section">
                        <h3>Last assistant turn</h3>
                        <p>
                          {lastAssistantText ||
                            "No recent assistant message available yet."}
                        </p>
                      </div>
                    </div>

                    {canReplyLive && (
                      <div
                        className="loop-inbox-reply-box"
                        data-testid="loop-reply-box"
                      >
                        <label
                          className="loop-inbox-reply-label"
                          htmlFor="loop-reply-input"
                        >
                          Reply to live session
                        </label>
                        <div className="loop-inbox-reply-controls">
                          <input
                            id="loop-reply-input"
                            className="loop-inbox-reply-input"
                            type="text"
                            value={replyText}
                            onChange={(event) =>
                              setReplyText(event.target.value)
                            }
                            placeholder="Tell the live session what to do next"
                            disabled={replyMutation.isPending}
                            data-testid="loop-reply-input"
                          />
                          <Button
                            variant="secondary"
                            disabled={
                              replyMutation.isPending || !replyText.trim()
                            }
                            onClick={() => {
                              if (!currentSession || !replyText.trim()) return;
                              void replyMutation.mutateAsync({
                                sessionId: currentSession.id,
                                message: replyText.trim(),
                              });
                            }}
                            data-testid="loop-reply-action"
                          >
                            {replyMutation.isPending ? "Sending…" : "Reply"}
                          </Button>
                        </div>
                      </div>
                    )}

                    <div className="loop-inbox-card-actions">
                      {currentStillActionable && (
                        <Button
                          variant="ghost"
                          disabled={snoozeMutation.isPending}
                          onClick={() => {
                            if (!currentSession) return;
                            void snoozeMutation.mutateAsync(currentSession.id);
                          }}
                          data-testid="loop-not-now-action"
                        >
                          {snoozeMutation.isPending ? "Updating…" : "Not now"}
                        </Button>
                      )}
                      {canResumeSession && (
                        <Button
                          variant="secondary"
                          disabled={resumeMutation.isPending}
                          onClick={() => {
                            if (!currentSession) return;
                            void resumeMutation.mutateAsync(currentSession.id);
                          }}
                          data-testid="loop-resume-action"
                        >
                          {resumeMutation.isPending ? "Updating…" : "Resume"}
                        </Button>
                      )}
                      <Link
                        className="ui-button ui-button--ghost ui-button--md"
                        to={`/timeline/${currentSession.id}`}
                      >
                        Open full session
                      </Link>
                    </div>
                  </>
                ) : (
                  <EmptyState
                    title="No session selected"
                    description="Open the queue to choose the next session waiting on you."
                  />
                )}
              </section>

              {installBanner}
            </>
          ) : (
            <div className="loop-inbox-layout">
              <section
                className="loop-inbox-list"
                aria-label="Sessions waiting on you"
              >
                <div className="loop-inbox-list-header">
                  <div>
                    <div className="loop-inbox-list-label">Phone queue</div>
                    <div className="loop-inbox-list-title">
                      {queueItems.length > 0
                        ? "Sessions waiting on you"
                        : "Nothing waiting right now"}
                    </div>
                  </div>
                  {queueItems.length > 0 && (
                    <span className="loop-inbox-list-count">
                      {queueItems.length}
                    </span>
                  )}
                </div>

                {queueItems.length > 0 ? (
                  <div className="loop-inbox-list-body">
                    {queueItems.map((item) => (
                      <LoopSessionRow
                        key={item.session.id}
                        item={item}
                        selected={item.session.id === selectedSessionId}
                        to={`/loop/${item.session.id}`}
                      />
                    ))}
                  </div>
                ) : (
                  <EmptyState
                    title="No waiting sessions"
                    description="When a live session blocks or asks for input, it will show up here."
                  />
                )}
              </section>

              <section
                className="loop-inbox-card"
                data-testid="loop-inbox-card"
              >
                {showStaleBanner && (
                  <div
                    className="loop-inbox-card-status-banner"
                    data-testid="loop-inbox-card-status-banner"
                  >
                    <div className="loop-inbox-card-status-banner-copy">
                      <div className="loop-inbox-list-label">
                        Current status
                      </div>
                      <h3>{statusHeadline(currentSession)}</h3>
                      <p>{statusDescription(currentSession)}</p>
                    </div>
                    {currentRecoverySessionId &&
                      currentRecoverySessionId !== selectedSessionId && (
                        <Link
                          className="ui-button ui-button--secondary ui-button--sm"
                          to={`/loop/${currentRecoverySessionId}`}
                        >
                          Open current
                        </Link>
                      )}
                  </div>
                )}

                {isLoadingCard && !currentSession ? (
                  <div className="loop-inbox-card-loading">
                    <Spinner size="sm" />
                    <span>Loading session…</span>
                  </div>
                ) : currentSession ? (
                  <>
                    <div className="loop-inbox-card-top">
                      <div>
                        <h2>{currentTitle}</h2>
                        <div className="loop-inbox-card-meta">
                          {currentVenueLabel && (
                            <span className="loop-inbox-home-label">
                              {currentVenueLabel}
                            </span>
                          )}
                          {currentSession.project && (
                            <span>{currentSession.project}</span>
                          )}
                          {currentMachineLabel && (
                            <span>{currentMachineLabel}</span>
                          )}
                          <span>
                            {formatTimestamp(
                              currentSession.timeline_anchor_at ||
                                currentSession.last_activity_at ||
                                currentSession.started_at,
                            )}
                          </span>
                        </div>
                      </div>
                      <div className="loop-inbox-card-top-badges">
                        {currentStillActionable ? (
                          <Badge
                            variant={attentionBadgeVariant(
                              currentAttentionState,
                            )}
                          >
                            {formatAttentionLabel(currentAttentionState)}
                          </Badge>
                        ) : (
                          <PresenceBadge
                            state={currentPresenceState}
                            tool={currentPresenceTool}
                          />
                        )}
                      </div>
                    </div>

                    <PresenceHero
                      state={currentPresenceState}
                      tool={currentPresenceTool}
                    />

                    <div className="loop-inbox-card-section">
                      <h3>What is happening</h3>
                      <p>{currentSummary}</p>
                    </div>

                    <div className="loop-inbox-card-context">
                      <div className="loop-inbox-card-section">
                        <h3>Last user instruction</h3>
                        <p>
                          {lastUserText ||
                            "No recent user message available yet."}
                        </p>
                      </div>
                      <div className="loop-inbox-card-section">
                        <h3>Last assistant turn</h3>
                        <p>
                          {lastAssistantText ||
                            "No recent assistant message available yet."}
                        </p>
                      </div>
                    </div>

                    {canReplyLive && (
                      <div
                        className="loop-inbox-reply-box"
                        data-testid="loop-reply-box"
                      >
                        <label
                          className="loop-inbox-reply-label"
                          htmlFor="loop-reply-input-desktop"
                        >
                          Reply to live session
                        </label>
                        <div className="loop-inbox-reply-controls">
                          <input
                            id="loop-reply-input-desktop"
                            className="loop-inbox-reply-input"
                            type="text"
                            value={replyText}
                            onChange={(event) =>
                              setReplyText(event.target.value)
                            }
                            placeholder="Tell the live session what to do next"
                            disabled={replyMutation.isPending}
                            data-testid="loop-reply-input"
                          />
                          <Button
                            variant="secondary"
                            disabled={
                              replyMutation.isPending || !replyText.trim()
                            }
                            onClick={() => {
                              if (!currentSession || !replyText.trim()) return;
                              void replyMutation.mutateAsync({
                                sessionId: currentSession.id,
                                message: replyText.trim(),
                              });
                            }}
                            data-testid="loop-reply-action"
                          >
                            {replyMutation.isPending ? "Sending…" : "Reply"}
                          </Button>
                        </div>
                      </div>
                    )}

                    <div className="loop-inbox-card-actions">
                      {currentStillActionable && (
                        <Button
                          variant="ghost"
                          disabled={snoozeMutation.isPending}
                          onClick={() => {
                            if (!currentSession) return;
                            void snoozeMutation.mutateAsync(currentSession.id);
                          }}
                          data-testid="loop-not-now-action"
                        >
                          {snoozeMutation.isPending ? "Updating…" : "Not now"}
                        </Button>
                      )}
                      {canResumeSession && (
                        <Button
                          variant="secondary"
                          disabled={resumeMutation.isPending}
                          onClick={() => {
                            if (!currentSession) return;
                            void resumeMutation.mutateAsync(currentSession.id);
                          }}
                          data-testid="loop-resume-action"
                        >
                          {resumeMutation.isPending ? "Updating…" : "Resume"}
                        </Button>
                      )}
                      <Link
                        className="ui-button ui-button--ghost ui-button--md"
                        to={`/timeline/${currentSession.id}`}
                      >
                        Open full session
                      </Link>
                    </div>
                  </>
                ) : (
                  <EmptyState
                    title="No session selected"
                    description="Choose a session from the queue to open its phone view."
                  />
                )}
              </section>
            </div>
          ))}

        {mobileQueueDrawer}
      </div>
    </PageShell>
  );
}
