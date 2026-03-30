import { Badge } from "../ui";
import type { SessionTurnReview } from "../../services/api/oikos";
import { formatFullDate } from "../../lib/sessionWorkspace";

const TURN_DECISION_META: Record<string, { label: string; variant: "neutral" | "warning" | "success" }> = {
  continue: { label: "Continue", variant: "success" },
  ask_user: { label: "Ask You", variant: "warning" },
  wait: { label: "Wait", variant: "neutral" },
  done: { label: "Done", variant: "neutral" },
  escalate: { label: "Escalate", variant: "warning" },
};

const EXECUTION_STATE_META: Record<string, { label: string; variant: "neutral" | "warning" | "success" }> = {
  observe_only: { label: "Observe Only", variant: "neutral" },
  awaiting_user_approval: { label: "Ask You", variant: "warning" },
  would_auto_continue: { label: "Would Auto-Continue", variant: "success" },
  needs_human: { label: "Needs You", variant: "warning" },
  no_action: { label: "No Action", variant: "neutral" },
};

function formatRecommendedAction(value: string | null): string | null {
  if (!value) return null;
  return value
    .split("_")
    .map((part) => (part ? part[0].toUpperCase() + part.slice(1) : part))
    .join(" ");
}

function formatLatency(value: number | null): string | null {
  if (value == null || !Number.isFinite(value) || value < 0) return null;
  if (value < 1000) return `${Math.round(value)} ms`;
  const seconds = value / 1000;
  return `${seconds >= 10 ? seconds.toFixed(0) : seconds.toFixed(1)} s`;
}

interface TurnReviewCardProps {
  review: SessionTurnReview | null;
  loading?: boolean;
  unavailable?: boolean;
}

export function TurnReviewCard({ review, loading = false, unavailable = false }: TurnReviewCardProps) {
  const decisionMeta = review
    ? TURN_DECISION_META[review.decision] ?? { label: review.decision, variant: "neutral" as const }
    : null;
  const executionMeta = review
    ? EXECUTION_STATE_META[review.executionState] ?? { label: review.executionState, variant: "neutral" as const }
    : null;
  const recommendedAction = formatRecommendedAction(review?.recommendedAction ?? null);
  const actualOutcome = formatRecommendedAction(review?.actualOutcome ?? null);
  const followUpPrompt = review?.followUpPrompt?.trim() || null;
  const queueLatency = formatLatency(review?.queueLatencyMs ?? null);
  const reviewLatency = formatLatency(review?.reviewLatencyMs ?? null);
  const processingLatency = formatLatency(review?.processingLatencyMs ?? null);
  const debugPayload = review ? JSON.stringify(review, null, 2) : null;

  return (
    <div className="session-pane-section">
      <div className="session-pane-section-title">Turn Loop</div>
      {loading ? (
        <div className="session-shadow-review__empty">Loading latest completed-turn review...</div>
      ) : review ? (
        <div className="session-shadow-review" data-testid="session-turn-review">
          <div className="session-shadow-review__header">
            {decisionMeta ? <Badge variant={decisionMeta.variant}>{decisionMeta.label}</Badge> : null}
            {executionMeta ? <Badge variant={executionMeta.variant}>{executionMeta.label}</Badge> : null}
            <span className="session-shadow-review__stamp">{formatFullDate(review.createdAt)}</span>
          </div>
          <div className="session-shadow-review__summary">{review.summary}</div>
          {review.modeSummary ? (
            <div className="session-shadow-review__mode">{review.modeSummary}</div>
          ) : null}
          <div className="session-shadow-review__meta">
            Latest assistant turn #{review.turnIndex + 1}
          </div>
          {recommendedAction ? (
            <div className="session-shadow-review__meta">Recommended action: {recommendedAction}</div>
          ) : null}
          {followUpPrompt ? (
            <div className="session-shadow-review__meta">Suggested next prompt: {followUpPrompt}</div>
          ) : null}
          {actualOutcome ? (
            <div className="session-shadow-review__meta">Live outcome: {actualOutcome}</div>
          ) : null}
          {reviewLatency ? (
            <div className="session-shadow-review__meta">Review recorded in {reviewLatency}</div>
          ) : null}
          {queueLatency ? (
            <div className="session-shadow-review__meta">Queue delay before turn-loop: {queueLatency}</div>
          ) : null}
          {processingLatency ? (
            <div className="session-shadow-review__meta">Turn-loop processing time: {processingLatency}</div>
          ) : null}
          {review.turnExcerpt ? (
            <div className="session-shadow-review__mode">{review.turnExcerpt}</div>
          ) : null}
          {review.blockedReasons.length > 0 ? (
            <div className="session-shadow-review__blockers">
              {review.blockedReasons.map((reason) => (
                <div key={reason} className="session-shadow-review__blocker">{reason}</div>
              ))}
            </div>
          ) : null}
          {debugPayload ? (
            <details className="session-pane-callout session-pane-callout--muted" data-testid="session-turn-review-debug">
              <summary className="session-pane-callout-title">Debug review payload</summary>
              <pre className="inspector-code-block">
                <code>{debugPayload}</code>
              </pre>
            </details>
          ) : null}
        </div>
      ) : (
        <div className="session-shadow-review__empty">
          {unavailable
            ? "Turn-loop review is unavailable right now."
            : "No completed-turn review has been recorded for this session yet."}
        </div>
      )}
    </div>
  );
}
