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
  onAction: (action: LoopInboxAction) => void;
  pending: boolean;
}) {
  return (
    <div className="loop-inbox-card-actions">
      {card.availableActions.includes("approve_recommended_action") && (
        <Button
          onClick={() => onAction("approve_recommended_action")}
          disabled={pending}
          data-testid="loop-approve-action"
        >
          {primaryActionLabel(card)}
        </Button>
      )}
      {card.availableActions.includes("not_now") && (
        <Button
          variant="ghost"
          onClick={() => onAction("not_now")}
          disabled={pending}
          data-testid="loop-not-now-action"
        >
          Not now
        </Button>
      )}
      {card.cardState === "superseded" && card.supersededByCardId && (
        <Link className="ui-button ui-button--ghost ui-button--md" to={`/loop/card/${card.supersededByCardId}`}>
          Open latest
        </Link>
      )}
      <Link className="ui-button ui-button--ghost ui-button--md" to={`/timeline/${card.sessionId}`}>
        Open full session
      </Link>
    </div>
  );
}

function LoopInboxRow({
  item,
  selected,
  to,
  onSelect,
}: {
  item: LoopInboxItem;
  selected: boolean;
  to: string;
  onSelect?: () => void;
}) {
  return (
    <Link
      to={to}
      className={`loop-inbox-row loop-inbox-row--${item.decision.replace(/_/g, "-")}${selected ? " is-selected" : ""}`}
      onClick={onSelect}
      aria-current={selected ? "page" : undefined}
      data-testid={`loop-inbox-row-${item.cardId}`}
    >
      <div className="loop-inbox-row-top">
        <strong>{item.title}</strong>
        <Badge variant={decisionBadgeVariant(item.decision)}>{formatDecision(item.decision)}</Badge>
      </div>
      <div className="loop-inbox-row-meta">
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

  const selectedCardId = cardId ? Number(cardId) : null;
  const selectedSessionId = !selectedCardId && sessionId ? sessionId : null;

  const inboxQuery = useQuery({
    queryKey: ["loop-inbox"],
    queryFn: fetchLoopInbox,
    refetchInterval: 15000,
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
    mutationFn: ({ currentCardId, action }: { currentCardId: number; action: LoopInboxAction }) =>
      applyLoopInboxAction(currentCardId, action),
    onSuccess: async (_result, variables) => {
      await queryClient.invalidateQueries({ queryKey: ["loop-inbox"] });
      await queryClient.invalidateQueries({ queryKey: ["loop-action-card", variables.currentCardId] });
    },
  });

  const handleAction = (action: LoopInboxAction) => {
    if (!selectedCardId) return;
    actionMutation.mutate({ currentCardId: selectedCardId, action });
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
  const isLoadingCard = cardQuery.isLoading || legacySessionQuery.isLoading;
  const inboxItems = inboxQuery.data ?? [];
  const inboxCount = inboxItems.length;
  const hasInboxItems = inboxCount > 0;
  const initialCardId = !selectedCardId && !selectedSessionId ? inboxItems[0]?.cardId ?? null : null;
  const selectedQueueIndex = selectedCardId ? inboxItems.findIndex((item) => item.cardId === selectedCardId) : -1;
  const showMobileQueueToggle = isPhoneLayout && inboxCount > 1;
  const showCondensedMobileChrome = isPhoneLayout && Boolean(currentCard || isLoadingCard || selectedCardId || selectedSessionId);
  const showInstallBanner = !isInstalled && (canInstall || showIosHint);
  const showPushBanner = loopPush.error || (loopPush.enabledInBackend && loopPush.supported) || loopPush.isEnabled;
  const queueCountLabel = hasInboxItems ? formatFollowUpCount(inboxCount) : null;
  const queuePositionLabel = selectedQueueIndex >= 0 ? `Viewing ${selectedQueueIndex + 1} of ${inboxCount}` : null;
  const mobileHeaderDetail = showMobileQueueToggle ? queuePositionLabel ?? queueCountLabel : queueCountLabel;
  const queueSummaryLabel =
    queueCountLabel && queuePositionLabel ? `${queueCountLabel} · ${queuePositionLabel}` : queueCountLabel;

  useEffect(() => {
    if (!showMobileQueueToggle) {
      setQueueOpen(false);
    }
  }, [showMobileQueueToggle]);

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
          <div className="loop-inbox-card-top">
            <div>
              <h2>{currentCard.title}</h2>
              <div className="loop-inbox-card-meta">
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

          {currentCard.cardStateReason && (
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
            On iPhone: tap Share, then <strong>Add to Home Screen</strong>.
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

  return (
    <PageShell size="wide" className="loop-inbox-shell">
      <div className="loop-inbox-page">
        {showCondensedMobileChrome ? (
          <div className="loop-inbox-mobile-header" data-testid="loop-mobile-header">
            <div className="loop-inbox-mobile-header-copy">
              <div className="loop-inbox-mobile-header-label">Loop Inbox</div>
              {mobileHeaderDetail && <div className="loop-inbox-mobile-header-detail">{mobileHeaderDetail}</div>}
            </div>
            {showMobileQueueToggle && (
              <Button
                variant="secondary"
                size="sm"
                className="loop-inbox-mobile-followups-trigger"
                onClick={() => setQueueOpen(true)}
                aria-haspopup="dialog"
                aria-controls="loop-mobile-queue-drawer"
                aria-expanded={queueOpen}
                data-testid="loop-mobile-queue-toggle"
              >
                <SidebarIcon width={16} height={16} />
                <span className="loop-inbox-mobile-followups-trigger-label">Follow-ups</span>
                <span className="loop-inbox-mobile-queue-trigger-count" aria-hidden="true">
                  {inboxCount}
                </span>
              </Button>
            )}
          </div>
        ) : (
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
              {cardPanel}
              {pushBanner}
              {installBanner}

              {showMobileQueueToggle && queueOpen && (
                <>
                  <div
                    className="loop-inbox-queue-drawer-scrim"
                    data-testid="loop-mobile-queue-scrim"
                    onClick={() => setQueueOpen(false)}
                    aria-hidden="true"
                  />
                  <aside
                    id="loop-mobile-queue-drawer"
                    className="loop-inbox-queue-drawer"
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby="loop-mobile-queue-drawer-title"
                    data-testid="loop-mobile-queue-drawer"
                  >
                    <div className="loop-inbox-queue-drawer-header">
                      <div>
                        <div className="loop-inbox-list-label">Loop Inbox</div>
                        <h2 id="loop-mobile-queue-drawer-title" className="loop-inbox-queue-drawer-title">
                          Follow-ups
                        </h2>
                        {queueSummaryLabel && (
                          <p className="loop-inbox-queue-drawer-summary">{queueSummaryLabel}</p>
                        )}
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
                      {inboxItems.map((item) => (
                        <LoopInboxRow
                          key={item.cardId}
                          item={item}
                          selected={item.cardId === selectedCardId}
                          to={buildLoopCardPath(item.cardId)}
                          onSelect={() => setQueueOpen(false)}
                        />
                      ))}
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
              )}
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
      </div>
    </PageShell>
  );
}
