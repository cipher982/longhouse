import { Badge, Button } from "../ui";
import type { AgentSession, SessionLoopMode } from "../../services/api/agents";
import type { SessionTurnReview } from "../../services/api/oikos";
import {
  formatContinuationStamp,
  formatDuration,
  formatProviderLabel,
  formatFullDate,
  getProviderColor,
  getSessionOriginLabel,
  truncatePath,
} from "../../lib/sessionWorkspace";

interface SessionContextPaneProps {
  session: AgentSession;
  title: string;
  headThreadSession: AgentSession | null;
  threadSessions: AgentSession[];
  isViewingHead: boolean;
  onOpenSession: (sessionId: string) => void;
  onOpenLatest: () => void;
  continuationNotice?: {
    title: string;
    body: string;
  } | null;
  loopModePending?: boolean;
  onLoopModeChange?: (nextMode: SessionLoopMode) => void;
  latestTurnReview?: SessionTurnReview | null;
  turnReviewLoading?: boolean;
  turnReviewUnavailable?: boolean;
}

const LOOP_MODE_OPTIONS: Array<{
  value: SessionLoopMode;
  label: string;
  hint: string;
}> = [
  { value: "manual", label: "Manual", hint: "Observe only" },
  { value: "assist", label: "Assist", hint: "Suggest and nudge" },
  { value: "autopilot", label: "Autopilot", hint: "Continue bounded turns" },
];

const TURN_DECISION_META: Record<string, { label: string; variant: "neutral" | "warning" | "success" }> = {
  continue: { label: "Continue", variant: "success" },
  ask_user: { label: "Ask You", variant: "warning" },
  wait: { label: "Wait", variant: "neutral" },
  done: { label: "Done", variant: "neutral" },
  escalate: { label: "Escalate", variant: "warning" },
};

const EXECUTION_STATE_META: Record<
  string,
  { label: string; variant: "neutral" | "warning" | "success" }
> = {
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

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="session-context-meta-row">
      <span className="session-context-meta-label">{label}</span>
      <span className="session-context-meta-value">{value}</span>
    </div>
  );
}

export function SessionContextPane({
  session,
  title,
  headThreadSession,
  threadSessions,
  isViewingHead,
  onOpenSession,
  onOpenLatest,
  continuationNotice = null,
  loopModePending = false,
  onLoopModeChange,
  latestTurnReview = null,
  turnReviewLoading = false,
  turnReviewUnavailable = false,
}: SessionContextPaneProps) {
  const turnCount = session.user_messages + session.assistant_messages;
  const decisionMeta = latestTurnReview
    ? TURN_DECISION_META[latestTurnReview.decision] ?? {
        label: latestTurnReview.decision,
        variant: "neutral" as const,
      }
    : null;
  const executionMeta = latestTurnReview
    ? EXECUTION_STATE_META[latestTurnReview.executionState] ?? {
        label: latestTurnReview.executionState,
        variant: "neutral" as const,
      }
    : null;
  const recommendedAction = formatRecommendedAction(latestTurnReview?.recommendedAction ?? null);
  const actualOutcome = formatRecommendedAction(latestTurnReview?.actualOutcome ?? null);

  return (
    <div className="session-context-pane">
      <div className="session-pane-section session-pane-section--hero">
        <div className="session-pane-eyebrow">Session</div>
        <div className="session-context-title">{title}</div>
        <div className="session-context-subtitle">
          <span className="session-context-provider">
            <span
              className="session-context-provider-dot"
              style={{ backgroundColor: getProviderColor(session.provider) }}
            />
            {formatProviderLabel(session.provider)}
          </span>
          <span>{session.ended_at ? "Completed" : "In Progress"}</span>
        </div>
        <div className="session-context-badges">
          <Badge variant="neutral">{turnCount} turns</Badge>
          <Badge variant="neutral">{session.tool_calls} tools</Badge>
          {session.environment && session.environment !== "production" ? (
            <Badge variant="warning">{session.environment}</Badge>
          ) : null}
        </div>
      </div>

      {!isViewingHead && headThreadSession ? (
        <div
          className="session-pane-callout session-pane-callout--warning session-branch-banner"
          data-testid="session-branch-banner"
        >
          <div className="session-pane-callout-title">This is not the latest continuation</div>
          <div className="session-pane-callout-copy">
            Latest head: {getSessionOriginLabel(headThreadSession)} from{" "}
            {formatContinuationStamp(headThreadSession.started_at)}.
          </div>
          <Button variant="secondary" size="sm" onClick={onOpenLatest}>
            Open Latest
          </Button>
        </div>
      ) : null}

      <div className="session-pane-section">
        <div className="session-pane-section-title">Metadata</div>
        <div className="session-context-meta">
          <MetaRow label="Started" value={formatFullDate(session.started_at)} />
          <MetaRow label="Duration" value={formatDuration(session.started_at, session.ended_at)} />
          {session.git_branch ? <MetaRow label="Branch" value={session.git_branch} /> : null}
          {session.cwd ? <MetaRow label="Workspace" value={truncatePath(session.cwd, 60)} /> : null}
          {session.project ? <MetaRow label="Project" value={session.project} /> : null}
        </div>
      </div>

      <div className="session-pane-section">
        <div className="session-pane-section-title">Loop Mode</div>
        <div
          className="session-loop-mode"
          role="radiogroup"
          aria-label="Session loop mode"
          data-testid="session-loop-mode-group"
        >
          {LOOP_MODE_OPTIONS.map((option) => {
            const isActive = session.loop_mode === option.value;
            return (
              <button
                key={option.value}
                type="button"
                role="radio"
                aria-checked={isActive}
                className={`session-loop-mode__option${isActive ? " is-active" : ""}`}
                onClick={() => onLoopModeChange?.(option.value)}
                disabled={loopModePending || !onLoopModeChange}
              >
                <span className="session-loop-mode__label">{option.label}</span>
                <span className="session-loop-mode__hint">{option.hint}</span>
              </button>
            );
          })}
        </div>
        <div className="session-loop-mode__caption">
          Stored session preference for what Oikos may do after each completed assistant turn.
        </div>
      </div>

      <div className="session-pane-section">
        <div className="session-pane-section-title">Turn Loop</div>
        {turnReviewLoading ? (
          <div className="session-shadow-review__empty">Loading latest completed-turn review...</div>
        ) : latestTurnReview ? (
          <div className="session-shadow-review" data-testid="session-turn-review">
            <div className="session-shadow-review__header">
              {decisionMeta ? <Badge variant={decisionMeta.variant}>{decisionMeta.label}</Badge> : null}
              {executionMeta ? <Badge variant={executionMeta.variant}>{executionMeta.label}</Badge> : null}
              <span className="session-shadow-review__stamp">{formatFullDate(latestTurnReview.createdAt)}</span>
            </div>
            <div className="session-shadow-review__summary">{latestTurnReview.summary}</div>
            {latestTurnReview.modeSummary ? (
              <div className="session-shadow-review__mode">{latestTurnReview.modeSummary}</div>
            ) : null}
            <div className="session-shadow-review__meta">
              Latest assistant turn #{latestTurnReview.turnIndex + 1}
            </div>
            {recommendedAction ? (
              <div className="session-shadow-review__meta">Recommended action: {recommendedAction}</div>
            ) : null}
            {actualOutcome ? (
              <div className="session-shadow-review__meta">Live outcome: {actualOutcome}</div>
            ) : null}
            {latestTurnReview.turnExcerpt ? (
              <div className="session-shadow-review__mode">{latestTurnReview.turnExcerpt}</div>
            ) : null}
            {latestTurnReview.blockedReasons.length > 0 ? (
              <div className="session-shadow-review__blockers">
                {latestTurnReview.blockedReasons.map((reason) => (
                  <div key={reason} className="session-shadow-review__blocker">
                    {reason}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ) : (
          <div className="session-shadow-review__empty">
            {turnReviewUnavailable
              ? "Turn-loop review is unavailable right now."
              : "No completed-turn review has been recorded for this session yet."}
          </div>
        )}
      </div>

      {continuationNotice ? (
        <div
          className="session-pane-callout session-pane-callout--muted"
          data-testid="session-continuation-unavailable"
        >
          <div className="session-pane-callout-title">{continuationNotice.title}</div>
          <div className="session-pane-callout-copy">{continuationNotice.body}</div>
        </div>
      ) : null}

      {session.summary ? (
        <div className="session-pane-section">
          <div className="session-pane-section-title">Summary</div>
          <div className="session-context-summary">{session.summary}</div>
        </div>
      ) : null}

      {threadSessions.length > 1 ? (
        <div
          className="session-pane-section session-pane-section--grow session-lineage-panel"
          data-testid="session-lineage-panel"
        >
          <div className="session-pane-section-title">Continuations</div>
          <div className="session-context-thread-list">
            {threadSessions.map((threadSession) => {
              const isCurrent = threadSession.id === session.id;
              const isHead = threadSession.id === headThreadSession?.id;
              return (
                <button
                  key={threadSession.id}
                  type="button"
                  className={`session-context-thread-item${isCurrent ? " is-current" : ""}${isHead ? " is-head" : ""}`}
                  onClick={() => onOpenSession(threadSession.id)}
                >
                  <div className="session-context-thread-item-title">
                    {getSessionOriginLabel(threadSession)}
                  </div>
                  <div className="session-context-thread-item-meta">
                    {formatContinuationStamp(threadSession.started_at)}
                  </div>
                  <div className="session-context-thread-item-badges">
                    {isHead ? <Badge variant="success">Latest</Badge> : null}
                    {isCurrent ? <Badge variant="neutral">Viewing</Badge> : null}
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}
