import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Badge, Button, EmptyState, PageShell, Spinner } from "../components/ui";
import {
  applyLoopInboxAction,
  fetchLoopActionCard,
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
      data-testid={`loop-inbox-row-${item.sessionId}`}
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
  const { sessionId } = useParams<{ sessionId?: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const inboxQuery = useQuery({
    queryKey: ["loop-inbox"],
    queryFn: fetchLoopInbox,
    refetchInterval: 15000,
  });

  const selectedSessionId = sessionId ?? null;

  useEffect(() => {
    if (selectedSessionId || !inboxQuery.data || inboxQuery.data.length === 0) return;
    navigate(`/loop/${inboxQuery.data[0].sessionId}`, { replace: true });
  }, [inboxQuery.data, navigate, selectedSessionId]);

  const cardQuery = useQuery({
    queryKey: ["loop-action-card", selectedSessionId],
    queryFn: () => fetchLoopActionCard(selectedSessionId as string),
    enabled: Boolean(selectedSessionId),
  });

  const actionMutation = useMutation({
    mutationFn: ({ currentSessionId, action }: { currentSessionId: string; action: LoopInboxAction }) =>
      applyLoopInboxAction(currentSessionId, action),
    onSuccess: async (_result, variables) => {
      await queryClient.invalidateQueries({ queryKey: ["loop-inbox"] });
      await queryClient.invalidateQueries({ queryKey: ["loop-action-card", variables.currentSessionId] });
      const nextInbox = await queryClient.fetchQuery({
        queryKey: ["loop-inbox"],
        queryFn: fetchLoopInbox,
      });
      const stillSelected = nextInbox.some((item) => item.sessionId === variables.currentSessionId);
      if (stillSelected) return;
      navigate(nextInbox[0] ? `/loop/${nextInbox[0].sessionId}` : "/loop", { replace: true });
    },
  });

  const handleAction = (action: LoopInboxAction) => {
    if (!selectedSessionId) return;
    actionMutation.mutate({ currentSessionId: selectedSessionId, action });
  };

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

        {inboxQuery.isLoading && (
          <div className="loop-inbox-loading">
            <Spinner size="md" />
            <span>Loading loop inbox…</span>
          </div>
        )}

        {!inboxQuery.isLoading && inboxQuery.data?.length === 0 && (
          <EmptyState
            title="No sessions need attention"
            description="Finished turns that need approval or review will appear here."
          />
        )}

        {!inboxQuery.isLoading && (inboxQuery.data?.length ?? 0) > 0 && (
          <div className="loop-inbox-layout">
            <section className="loop-inbox-list" aria-label="Sessions needing attention">
              {inboxQuery.data?.map((item) => (
                <LoopInboxRow
                  key={item.sessionId}
                  item={item}
                  selected={item.sessionId === selectedSessionId}
                  onSelect={() => navigate(`/loop/${item.sessionId}`)}
                />
              ))}
            </section>

            <section className="loop-inbox-card" data-testid="loop-inbox-card">
              {cardQuery.isLoading && selectedSessionId && (
                <div className="loop-inbox-card-loading">
                  <Spinner size="sm" />
                  <span>Loading action card…</span>
                </div>
              )}

              {!selectedSessionId && (
                <EmptyState
                  title="Select a session"
                  description="Choose a session from the inbox to review the recommended next action."
                />
              )}

              {cardQuery.data && (
                <>
                  <div className="loop-inbox-card-top">
                    <div>
                      <h2>{cardQuery.data.title}</h2>
                      <div className="loop-inbox-card-meta">
                        <span>{cardQuery.data.project || "No project"}</span>
                        {cardQuery.data.machine && <span>{cardQuery.data.machine}</span>}
                        <span>{formatTimestamp(cardQuery.data.lastTurnAt)}</span>
                      </div>
                    </div>
                    <Badge variant="neutral">{formatDecision(cardQuery.data.decision)}</Badge>
                  </div>

                  <div className="loop-inbox-card-section">
                    <h3>What happened</h3>
                    <p>{cardQuery.data.summary}</p>
                  </div>

                  {cardQuery.data.followUpPrompt && (
                    <div className="loop-inbox-card-section">
                      <h3>Recommended next prompt</h3>
                      <p>{cardQuery.data.followUpPrompt}</p>
                    </div>
                  )}

                  {cardQuery.data.rationale && (
                    <div className="loop-inbox-card-section">
                      <h3>Why</h3>
                      <p>{cardQuery.data.rationale}</p>
                    </div>
                  )}

                  <div className="loop-inbox-card-context">
                    {cardQuery.data.lastUserText && (
                      <div className="loop-inbox-card-section">
                        <h3>Last user instruction</h3>
                        <p>{cardQuery.data.lastUserText}</p>
                      </div>
                    )}
                    {cardQuery.data.lastAssistantText && (
                      <div className="loop-inbox-card-section">
                        <h3>Last assistant turn</h3>
                        <p>{cardQuery.data.lastAssistantText}</p>
                      </div>
                    )}
                  </div>

                  {cardQuery.data.blockedReasons.length > 0 && (
                    <div className="loop-inbox-card-section">
                      <h3>Blocked reasons</h3>
                      <ul className="loop-inbox-bullets">
                        {cardQuery.data.blockedReasons.map((reason) => (
                          <li key={reason}>{reason}</li>
                        ))}
                      </ul>
                    </div>
                  )}

                  <LoopActionButtons
                    card={cardQuery.data}
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
