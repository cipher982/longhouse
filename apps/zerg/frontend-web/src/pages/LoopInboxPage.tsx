import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
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
import "../styles/loop-inbox.css";

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
  onSelect,
}: {
  item: LoopInboxItem;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      className={`loop-inbox-row${selected ? " is-selected" : ""}`}
      onClick={onSelect}
      data-testid={`loop-inbox-row-${item.cardId}`}
    >
      <div className="loop-inbox-row-top">
        <strong>{item.title}</strong>
        <Badge variant="neutral">{formatDecision(item.decision)}</Badge>
      </div>
      <div className="loop-inbox-row-meta">
        <span>{item.project || "No project"}</span>
        {item.machine && <span>{item.machine}</span>}
        <span>{formatTimestamp(item.lastTurnAt)}</span>
      </div>
      <p>{item.summary}</p>
    </button>
  );
}

export default function LoopInboxPage() {
  const { sessionId, cardId } = useParams<{ sessionId?: string; cardId?: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const selectedCardId = cardId ? Number(cardId) : null;
  const selectedSessionId = !selectedCardId && sessionId ? sessionId : null;

  const inboxQuery = useQuery({
    queryKey: ["loop-inbox"],
    queryFn: fetchLoopInbox,
    refetchInterval: 15000,
  });

  useEffect(() => {
    if (selectedCardId || selectedSessionId || !inboxQuery.data || inboxQuery.data.length === 0) return;
    navigate(`/loop/card/${inboxQuery.data[0].cardId}`, { replace: true });
  }, [inboxQuery.data, navigate, selectedCardId, selectedSessionId]);

  const legacySessionQuery = useQuery({
    queryKey: ["loop-action-card-session", selectedSessionId],
    queryFn: () => fetchLoopActionCardForSession(selectedSessionId as string),
    enabled: Boolean(selectedSessionId),
  });

  useEffect(() => {
    if (!selectedSessionId || !legacySessionQuery.data) return;
    navigate(`/loop/card/${legacySessionQuery.data.cardId}`, { replace: true });
  }, [legacySessionQuery.data, navigate, selectedSessionId]);

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

  const currentCard = cardQuery.data ?? null;
  const isLoadingCard = cardQuery.isLoading || legacySessionQuery.isLoading;
  const hasInboxItems = (inboxQuery.data?.length ?? 0) > 0;

  return (
    <PageShell size="normal">
      <div className="loop-inbox-page">
        <header className="loop-inbox-header">
          <div>
            <h1>Loop Inbox</h1>
            <p>Handle finished coding turns without opening the full desktop workspace.</p>
          </div>
          <Link className="ui-button ui-button--ghost ui-button--md" to="/timeline">
            Open timeline
          </Link>
        </header>

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
          <div className="loop-inbox-layout">
            <section className="loop-inbox-list" aria-label="Sessions needing attention">
              {hasInboxItems ? (
                inboxQuery.data?.map((item) => (
                  <LoopInboxRow
                    key={item.cardId}
                    item={item}
                    selected={item.cardId === selectedCardId}
                    onSelect={() => navigate(`/loop/card/${item.cardId}`)}
                  />
                ))
              ) : (
                <EmptyState
                  title="No active cards"
                  description="This follow-up still has details, but nothing else currently needs action."
                />
              )}
            </section>

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
                      <Badge variant="neutral">{formatDecision(currentCard.decision)}</Badge>
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
          </div>
        )}
      </div>
    </PageShell>
  );
}
