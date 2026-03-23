import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Navigate, useParams } from "react-router-dom";
import { SidebarIcon, XIcon } from "../components/icons";
import { Badge, Button, EmptyState, PageShell, Spinner } from "../components/ui";
import {
  applyLoopInboxAction,
  fetchLoopActionCard,
  fetchLoopActionCardForSession,
  fetchLoopInbox,
  type LoopActionCard,
  type LoopInboxAction,
  type LoopInboxItem,
} from "../services/api/oikos";
import { useLoopInstallPrompt } from "../hooks/useLoopInstallPrompt";
import { useLoopInboxStream } from "../hooks/useLoopInboxStream";
import { useLoopPushNotifications } from "../hooks/useLoopPushNotifications";
import "../styles/loop-inbox.css";

type DecisionBadgeVariant = "neutral" | "success" | "warning" | "error";
const PHONE_BREAKPOINT_PX = 768;

function formatDecision(decision: string): string {
  switch (decision) {
    case "continue":
      return "Continue";
    case "ask_user":
      return "Needs approval";
    case "wait":
      return "Wait";
    case "done":
      return "Done";
    case "escalate":
      return "Escalate";
    default:
      return decision.replace(/_/g, " ");
  }
}

function formatCardState(state: string): string {
  switch (state) {
    case "active":
      return "Active";
    case "acted":
      return "Handled";
    case "dismissed":
      return "Dismissed";
    case "superseded":
      return "Superseded";
    case "expired":
      return "Expired";
    case "failed":
      return "Failed";
    default:
      return state.replace(/_/g, " ");
  }
}

function decisionBadgeVariant(decision: string): DecisionBadgeVariant {
  switch (decision) {
    case "continue":
    case "done":
      return "success";
    case "ask_user":
    case "wait":
      return "warning";
    case "escalate":
      return "error";
    default:
      return "neutral";
  }
}

function cardStateBadgeVariant(state: string): DecisionBadgeVariant {
  switch (state) {
    case "active":
      return "success";
    case "superseded":
    case "expired":
      return "warning";
    case "failed":
      return "error";
    default:
      return "neutral";
  }
}

function cardStatusHeadline(card: Pick<LoopActionCard, "cardState">): string {
  switch (card.cardState) {
    case "superseded":
      return "Viewing older card";
    case "expired":
      return "This follow-up expired";
    case "acted":
      return "This follow-up was already handled";
    case "dismissed":
      return "This follow-up was dismissed";
    case "failed":
      return "This follow-up failed";
    default:
      return "Current card status";
  }
}

function formatTimestamp(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "Just now";
  return parsed.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function primaryActionLabel(card: LoopActionCard): string {
  if (card.recommendedAction === "continue_session") {
    return "Continue";
  }
  return "Approve";
}

function formatFollowUpCount(count: number): string {
  return `${count} open follow-up${count === 1 ? "" : "s"}`;
}

function formatVenueLabel(item: Pick<LoopInboxItem, "executionHome" | "homeLabel">): string | null {
  if (item.homeLabel) {
    return item.homeLabel;
  }
  switch (item.executionHome) {
    case "managed_local":
      return "On this Mac";
    case "managed_hosted":
      return "Hosted";
    case "cloud_takeover":
      return "Moved to cloud";
    default:
      return null;
  }
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

function buildLoopCardPath(cardId: number): string {
  return `/loop/card/${cardId}`;
}

function LoopActionButtons({
  card,
  onAction,
  pending,
}: {
  card: LoopActionCard;
  onAction: (action: LoopInboxAction, options?: { replyText?: string | null }) => Promise<void>;
  pending: boolean;
}) {
  const [replyText, setReplyText] = useState("");
  const canReply = card.availableActions.includes("reply_to_session");
  const trimmedReply = replyText.trim();
  const venueLabel = formatVenueLabel(card);

  useEffect(() => {
    setReplyText("");
  }, [card.cardId]);

  const submitReply = async () => {
    if (!trimmedReply || pending) return;
    await onAction("reply_to_session", { replyText: trimmedReply });
    setReplyText("");
  };

  return (
    <div className="loop-inbox-card-actions-wrap">
      {canReply && (
        <div className="loop-inbox-reply-box" data-testid="loop-reply-box">
          <label className="loop-inbox-reply-label" htmlFor={`loop-reply-${card.cardId}`}>
            Reply to source session
          </label>
          <div className="loop-inbox-reply-controls">
            <input
              id={`loop-reply-${card.cardId}`}
              className="loop-inbox-reply-input"
              type="text"
              value={replyText}
              onChange={(event) => setReplyText(event.target.value)}
              placeholder={
                venueLabel === "On this Mac"
                  ? "Send a quick reply to the session on this Mac"
                  : "Send a quick reply to this session"
              }
              disabled={pending}
              data-testid="loop-reply-input"
            />
            <Button
              variant="secondary"
              onClick={() => void submitReply()}
              disabled={pending || !trimmedReply}
              data-testid="loop-reply-action"
            >
              Reply
            </Button>
          </div>
        </div>
      )}
      <div className="loop-inbox-card-actions">
      {card.availableActions.includes("approve_recommended_action") && (
        <Button
          onClick={() => void onAction("approve_recommended_action")}
          disabled={pending}
          data-testid="loop-approve-action"
        >
          {primaryActionLabel(card)}
        </Button>
      )}
      {card.availableActions.includes("not_now") && (
        <Button
          variant="ghost"
          onClick={() => void onAction("not_now")}
          disabled={pending}
          data-testid="loop-not-now-action"
        >
          Not now
        </Button>
      )}
      <Link className="ui-button ui-button--ghost ui-button--md" to={`/timeline/${card.sessionId}`}>
        Open full session
      </Link>
      </div>
    </div>
  );
}

function LoopInboxRow({
  item,
  selected,
  to,
  onSelect,
  compact = false,
}: {
  item: LoopInboxItem;
  selected: boolean;
  to: string;
  onSelect?: () => void;
  compact?: boolean;
}) {
  const venueLabel = formatVenueLabel(item);

  return (
    <Link
      to={to}
      className={`loop-inbox-row loop-inbox-row--${item.decision.replace(/_/g, "-")}${selected ? " is-selected" : ""}${
        compact ? " loop-inbox-row--compact" : ""
      }`}
      onClick={onSelect}
      aria-current={selected ? "page" : undefined}
      data-testid={`loop-inbox-row-${item.cardId}`}
    >
      <div className="loop-inbox-row-top">
        <strong>{item.title}</strong>
        <div className="loop-inbox-row-badges">
          <Badge variant={decisionBadgeVariant(item.decision)}>{formatDecision(item.decision)}</Badge>
          {item.cardState !== "active" && (
            <Badge variant={cardStateBadgeVariant(item.cardState)}>{formatCardState(item.cardState)}</Badge>
          )}
        </div>
      </div>
      <div className="loop-inbox-row-meta">
        {venueLabel && <span className="loop-inbox-home-label">{venueLabel}</span>}
        <span>{item.project || "No project"}</span>
        {item.machine && <span>{item.machine}</span>}
        <span>{formatTimestamp(item.lastTurnAt)}</span>
      </div>
      <p>{item.summary}</p>
    </Link>
  );
}

export default function LoopInboxPage() {
  const { sessionId, cardId } = useParams<{ sessionId?: string; cardId?: string }>();
  const queryClient = useQueryClient();
  const { canInstall, showIosHint, isInstalled, install } = useLoopInstallPrompt();
  const loopPush = useLoopPushNotifications({ isInstalled });
  const isPhoneLayout = useIsPhoneLayout();
  const [queueOpen, setQueueOpen] = useState(false);
  const [queueAutoOpenedCardId, setQueueAutoOpenedCardId] = useState<number | null>(null);

  const selectedCardId = cardId ? Number(cardId) : null;
  const selectedSessionId = !selectedCardId && sessionId ? sessionId : null;

  const inboxQuery = useQuery({
    queryKey: ["loop-inbox"],
    queryFn: fetchLoopInbox,
    refetchInterval: typeof EventSource === "undefined" ? 15000 : false,
  });

  const legacySessionQuery = useQuery({
    queryKey: ["loop-action-card-session", selectedSessionId],
    queryFn: () => fetchLoopActionCardForSession(selectedSessionId as string),
    enabled: Boolean(selectedSessionId),
  });

  const cardQuery = useQuery({
    queryKey: ["loop-action-card", selectedCardId],
    queryFn: () => fetchLoopActionCard(selectedCardId as number),
    enabled: Number.isFinite(selectedCardId),
  });

  const actionMutation = useMutation({
    mutationFn: ({
      currentCardId,
      action,
      replyText,
    }: {
      currentCardId: number;
      action: LoopInboxAction;
      replyText?: string | null;
    }) =>
      replyText == null
        ? applyLoopInboxAction(currentCardId, action)
        : applyLoopInboxAction(currentCardId, action, { replyText }),
    onSuccess: async (_result, variables) => {
      await queryClient.invalidateQueries({ queryKey: ["loop-inbox"] });
      await queryClient.invalidateQueries({ queryKey: ["loop-action-card", variables.currentCardId] });
    },
  });

  useLoopInboxStream({
    enabled: typeof EventSource !== "undefined",
    selectedCardId,
  });

  const handleAction = async (action: LoopInboxAction, options?: { replyText?: string | null }) => {
    if (!selectedCardId) return;
    await actionMutation.mutateAsync({ currentCardId: selectedCardId, action, replyText: options?.replyText });
  };

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

  const currentCard = cardQuery.data ?? null;
  const currentVenueLabel = currentCard ? formatVenueLabel(currentCard) : null;
  const isLoadingCard = cardQuery.isLoading || legacySessionQuery.isLoading;
  const inboxItems = inboxQuery.data ?? [];
  const inboxCount = inboxItems.length;
  const hasInboxItems = inboxCount > 0;
  const initialCardId = !selectedCardId && !selectedSessionId ? inboxItems[0]?.cardId ?? null : null;
  const selectedQueueIndex = selectedCardId ? inboxItems.findIndex((item) => item.cardId === selectedCardId) : -1;
  const showMobileQueueButton = isPhoneLayout;
  const showCondensedMobileChrome = isPhoneLayout && Boolean(currentCard || isLoadingCard || selectedCardId || selectedSessionId);
  const showInstallBanner = !isInstalled && (canInstall || showIosHint);
  const showPushBanner = loopPush.error || (loopPush.enabledInBackend && loopPush.supported) || loopPush.isEnabled;
  const queueCountLabel = inboxCount === 0 ? "No open follow-ups" : formatFollowUpCount(inboxCount);
  const queuePositionLabel = selectedQueueIndex >= 0 ? `Viewing ${selectedQueueIndex + 1} of ${inboxCount}` : null;
  const currentCardIsStale = Boolean(currentCard && currentCard.cardState !== "active");
  const currentCardNeedsQueueRecovery = currentCardIsStale && hasInboxItems && selectedQueueIndex < 0;
  const currentRecoveryCardId =
    currentCard?.supersededByCardId ?? (currentCardNeedsQueueRecovery ? inboxItems[0]?.cardId ?? null : null);
  const showMobileTopBar = isPhoneLayout;
  const showMobileStatusNotice = isPhoneLayout && currentCardIsStale;
  const queueSummaryLabel =
    queueCountLabel && queuePositionLabel ? `${queueCountLabel} · ${queuePositionLabel}` : queueCountLabel;
  const queueButtonLabel = "Follow-ups";
  const queueButtonAriaLabel = currentCardNeedsQueueRecovery
    ? ["Open follow-ups", queueCountLabel, "Viewing older card"].filter(Boolean).join(". ")
    : ["Open follow-ups", queueCountLabel, queuePositionLabel].filter(Boolean).join(". ");

  useEffect(() => {
    if (!(currentCardNeedsQueueRecovery && isPhoneLayout && selectedCardId != null)) {
      return;
    }
    if (queueAutoOpenedCardId === selectedCardId) {
      return;
    }
    setQueueOpen(true);
    setQueueAutoOpenedCardId(selectedCardId);
  }, [currentCardNeedsQueueRecovery, isPhoneLayout, queueAutoOpenedCardId, selectedCardId]);

  if (selectedSessionId && legacySessionQuery.data) {
    return <Navigate to={buildLoopCardPath(legacySessionQuery.data.cardId)} replace />;
  }

  if (initialCardId != null) {
    return <Navigate to={buildLoopCardPath(initialCardId)} replace />;
  }

  const cardPanel = (
    <section className="loop-inbox-card" data-testid="loop-inbox-card">
      {isLoadingCard && (
        <div className="loop-inbox-card-loading">
          <Spinner size="sm" />
          <span>Loading action card…</span>
        </div>
      )}

      {!selectedCardId && !selectedSessionId && !currentCard && !isLoadingCard && (
        <EmptyState
          title="Select a follow-up"
          description="Choose a card from the inbox to review the recommended next action."
        />
      )}

      {currentCard && (
        <>
          {!showMobileStatusNotice && currentCard.cardState !== "active" && (
            <div className="loop-inbox-card-status-banner" data-testid="loop-inbox-card-status-banner">
              <div className="loop-inbox-card-status-banner-copy">
                <div className="loop-inbox-list-label">Current card status</div>
                <h3>{cardStatusHeadline(currentCard)}</h3>
                {currentCard.cardStateReason && <p>{currentCard.cardStateReason}</p>}
              </div>
              {currentRecoveryCardId != null && currentRecoveryCardId !== selectedCardId && (
                <Link
                  className="ui-button ui-button--secondary ui-button--sm"
                  to={`/loop/card/${currentRecoveryCardId}`}
                  onClick={() => setQueueOpen(false)}
                >
                  Open current
                </Link>
              )}
            </div>
          )}

          <div className="loop-inbox-card-top">
            <div>
              <h2>{currentCard.title}</h2>
              <div className="loop-inbox-card-meta">
                {currentVenueLabel && <span className="loop-inbox-home-label">{currentVenueLabel}</span>}
                <span>{currentCard.project || "No project"}</span>
                {currentCard.machine && <span>{currentCard.machine}</span>}
                <span>{formatTimestamp(currentCard.lastTurnAt)}</span>
              </div>
            </div>
            <div className="loop-inbox-card-top-badges">
              <Badge variant={decisionBadgeVariant(currentCard.decision)}>
                {formatDecision(currentCard.decision)}
              </Badge>
              {currentCard.cardState !== "active" && (
                <Badge variant="warning">{formatCardState(currentCard.cardState)}</Badge>
              )}
            </div>
          </div>

          {currentCard.cardStateReason && currentCard.cardState === "active" && (
            <div className="loop-inbox-card-section">
              <h3>Status</h3>
              <p>{currentCard.cardStateReason}</p>
            </div>
          )}

          <div className="loop-inbox-card-section">
            <h3>What happened</h3>
            <p>{currentCard.summary}</p>
          </div>

          {currentCard.followUpPrompt && (
            <div className="loop-inbox-card-section">
              <h3>Recommended next prompt</h3>
              <p>{currentCard.followUpPrompt}</p>
            </div>
          )}

          {currentCard.rationale && (
            <div className="loop-inbox-card-section">
              <h3>Why</h3>
              <p>{currentCard.rationale}</p>
            </div>
          )}

          <div className="loop-inbox-card-context">
            {currentCard.lastUserText && (
              <div className="loop-inbox-card-section">
                <h3>Last user instruction</h3>
                <p>{currentCard.lastUserText}</p>
              </div>
            )}
            {currentCard.lastAssistantText && (
              <div className="loop-inbox-card-section">
                <h3>Last assistant turn</h3>
                <p>{currentCard.lastAssistantText}</p>
              </div>
            )}
          </div>

          {currentCard.blockedReasons.length > 0 && (
            <div className="loop-inbox-card-section">
              <h3>Blocked reasons</h3>
              <ul className="loop-inbox-bullets">
                {currentCard.blockedReasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </div>
          )}

          <LoopActionButtons
            card={currentCard}
            onAction={handleAction}
            pending={actionMutation.isPending}
          />
        </>
      )}
    </section>
  );

  const installBanner = showInstallBanner ? (
    <section
      className={`loop-install-banner${showCondensedMobileChrome ? " loop-install-banner--compact" : ""}`}
      data-testid="loop-install-banner"
    >
      <div>
        <strong>Install Loop</strong>
        <p>
          {showCondensedMobileChrome
            ? "Save this view to your home screen for faster approvals."
            : "Save this inbox to your home screen so approvals open fast even on weak mobile connections."}
        </p>
      </div>
      <div className="loop-install-banner-actions">
        {canInstall && (
          <Button onClick={() => void install()} data-testid="loop-install-action">
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

  const pushBanner = showPushBanner ? (
    <section
      className={`loop-push-banner${showCondensedMobileChrome ? " loop-push-banner--compact" : ""}`}
      data-testid="loop-push-banner"
    >
      <div>
        <strong>Loop notifications</strong>
        <p>
          {showCondensedMobileChrome
            ? "Turn on direct card alerts for this phone."
            : "Get a direct nudge to the exact action card when a coding turn needs approval, instead of checking the desktop app."}
        </p>
        {loopPush.isEnabled && (
          <p className="loop-push-status" data-testid="loop-push-enabled-copy">
            Notifications are on for this install.
          </p>
        )}
        {loopPush.error && (
          <p className="loop-push-error" data-testid="loop-push-error">
            {loopPush.error}
          </p>
        )}
      </div>
      <div className="loop-push-banner-actions">
        {loopPush.canEnable && (
          <Button
            onClick={() => void loopPush.enable()}
            disabled={loopPush.isBusy}
            data-testid="loop-push-enable-action"
          >
            {loopPush.isBusy ? "Enabling…" : "Enable notifications"}
          </Button>
        )}
        {loopPush.canDisable && (
          <Button
            variant="ghost"
            onClick={() => void loopPush.disable()}
            disabled={loopPush.isBusy}
            data-testid="loop-push-disable-action"
          >
            {loopPush.isBusy ? "Updating…" : "Disable notifications"}
          </Button>
        )}
      </div>
    </section>
  ) : null;

  const mobileQueueDrawer =
    showMobileQueueButton ? (
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
              <h2 id="loop-mobile-queue-drawer-title" className="loop-inbox-queue-drawer-title">
                Follow-ups
              </h2>
              {queueSummaryLabel && <p className="loop-inbox-queue-drawer-summary">{queueSummaryLabel}</p>}
            </div>
            <Button
              variant="ghost"
              size="sm"
              className="loop-inbox-queue-drawer-close"
              onClick={() => setQueueOpen(false)}
              aria-label="Close follow-ups"
              data-testid="loop-mobile-queue-close"
            >
              <XIcon width={18} height={18} />
            </Button>
          </div>

          <div className="loop-inbox-queue-drawer-body">
            {inboxQuery.isLoading && !hasInboxItems ? (
              <div
                className="loop-inbox-card-loading loop-inbox-queue-drawer-loading"
                data-testid="loop-mobile-queue-drawer-loading"
              >
                <Spinner size="sm" />
                <span>Loading follow-ups…</span>
              </div>
            ) : hasInboxItems ? (
              inboxItems.map((item) => (
                <LoopInboxRow
                  key={item.cardId}
                  item={item}
                  selected={item.cardId === selectedCardId}
                  to={buildLoopCardPath(item.cardId)}
                  onSelect={() => setQueueOpen(false)}
                  compact
                />
              ))
            ) : (
              <div className="loop-inbox-queue-drawer-empty" data-testid="loop-mobile-queue-drawer-empty">
                <strong>No follow-ups right now</strong>
                <p>New approvals will appear here as soon as a coding turn needs review.</p>
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

  return (
    <PageShell size="wide" className="loop-inbox-shell">
      <div className="loop-inbox-page">
        {showMobileTopBar && (
          <header className="loop-inbox-mobile-header" data-testid="loop-mobile-header">
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
                <SidebarIcon width={18} height={18} className="loop-inbox-mobile-header-trigger-icon" />
                <span className="loop-inbox-mobile-header-trigger-label">{queueButtonLabel}</span>
                <span
                  className={`loop-inbox-mobile-header-trigger-count${inboxCount === 0 ? " is-empty" : ""}`}
                  data-testid="loop-mobile-queue-count"
                  aria-hidden="true"
                >
                  {inboxCount}
                </span>
              </button>
            </div>
            <div className="loop-inbox-mobile-header-copy">
              <div className="loop-inbox-mobile-header-title">Loop Inbox</div>
            </div>
          </header>
        )}

        {!isPhoneLayout && (
          <header className="loop-inbox-header">
            <div className="loop-inbox-header-copy">
              <span className="loop-inbox-eyebrow">Mobile approvals</span>
              <h1>Loop Inbox</h1>
              <p>Handle finished coding turns without opening the full desktop workspace.</p>
            </div>
            <Link className="ui-button ui-button--ghost ui-button--md" to="/timeline">
              Open timeline
            </Link>
          </header>
        )}

        {!showCondensedMobileChrome && installBanner}
        {!showCondensedMobileChrome && pushBanner}

        {inboxQuery.isLoading && !currentCard && (
          <div className="loop-inbox-loading">
            <Spinner size="md" />
            <span>Loading loop inbox…</span>
          </div>
        )}

        {!inboxQuery.isLoading && !hasInboxItems && !currentCard && !isLoadingCard && (
          <EmptyState
            title="No sessions need attention"
            description="Finished turns that need approval or review will appear here."
          />
        )}

        {(!inboxQuery.isLoading || currentCard || isLoadingCard) && (hasInboxItems || currentCard || isLoadingCard) && (
          isPhoneLayout ? (
            <>
              {showMobileStatusNotice && currentCard && (
                <section className="loop-inbox-mobile-status" data-testid="loop-inbox-card-status-banner">
                  <div className="loop-inbox-mobile-status-copy">
                    <h3>{cardStatusHeadline(currentCard)}</h3>
                    {currentCard.cardStateReason && <p>{currentCard.cardStateReason}</p>}
                  </div>
                  {currentRecoveryCardId != null && currentRecoveryCardId !== selectedCardId && (
                    <Link
                      className="ui-button ui-button--secondary ui-button--sm"
                      to={`/loop/card/${currentRecoveryCardId}`}
                      onClick={() => setQueueOpen(false)}
                    >
                      Open current
                    </Link>
                  )}
                </section>
              )}

              {cardPanel}
              {pushBanner}
              {installBanner}
            </>
          ) : (
            <div className="loop-inbox-layout">
              <section className="loop-inbox-list" aria-label="Sessions needing attention">
                <div className="loop-inbox-list-header">
                  <div>
                    <div className="loop-inbox-list-label">Attention queue</div>
                    <div className="loop-inbox-list-title">
                      {hasInboxItems ? "Open follow-ups" : "Nothing actionable right now"}
                    </div>
                  </div>
                  {hasInboxItems && (
                    <span className="loop-inbox-list-count">
                      {inboxCount}
                    </span>
                  )}
                </div>

                {hasInboxItems ? (
                  <div className="loop-inbox-list-body">
                    {inboxItems.map((item) => (
                      <LoopInboxRow
                        key={item.cardId}
                        item={item}
                        selected={item.cardId === selectedCardId}
                        to={buildLoopCardPath(item.cardId)}
                      />
                    ))}
                  </div>
                ) : (
                  <EmptyState
                    title="No active cards"
                    description="This follow-up still has details, but nothing else currently needs action."
                  />
                )}
              </section>

              {cardPanel}
            </div>
          )
        )}
        {mobileQueueDrawer}
      </div>
    </PageShell>
  );
}
