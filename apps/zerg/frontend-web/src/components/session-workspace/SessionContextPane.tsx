import { Badge, Button } from "../ui";
import type { AgentSession, SessionLoopMode } from "../../services/api/agents";
import type { SessionShadowReview, SessionShadowRollup } from "../../services/api/oikos";
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
  latestShadowReview?: SessionShadowReview | null;
  shadowRollup?: SessionShadowRollup | null;
  shadowReviewLoading?: boolean;
  shadowReviewUnavailable?: boolean;
}

const LOOP_MODE_OPTIONS: Array<{
  value: SessionLoopMode;
  label: string;
  hint: string;
}> = [
  { value: "manual", label: "Manual", hint: "Observe only" },
  { value: "assist", label: "Assist", hint: "Suggest next steps" },
  { value: "autopilot", label: "Autopilot", hint: "Bounded continues" },
];

const EXECUTION_STATE_META: Record<
  string,
  { label: string; variant: "neutral" | "warning" | "success" }
> = {
  observe_only: { label: "Observe Only", variant: "neutral" },
  awaiting_user_approval: { label: "Awaiting Approval", variant: "warning" },
  would_auto_continue: { label: "Would Auto-Continue", variant: "success" },
  needs_human: { label: "Needs Human", variant: "warning" },
  no_action: { label: "No Action", variant: "neutral" },
};

const SHADOW_ALIGNMENT_META: Record<
  string,
  { label: string; variant: "neutral" | "warning" | "success" }
> = {
  matched: { label: "Matched Shadow", variant: "success" },
  more_conservative: { label: "More Conservative", variant: "neutral" },
  more_aggressive: { label: "More Aggressive", variant: "warning" },
  different: { label: "Different Outcome", variant: "warning" },
  failed: { label: "Run Failed", variant: "warning" },
};

const SHADOW_READINESS_META: Record<
  string,
  { label: string; variant: "neutral" | "warning" | "success" }
> = {
  no_signal: { label: "No Signal Yet", variant: "neutral" },
  early: { label: "Early Signal", variant: "neutral" },
  promising: { label: "Promising", variant: "success" },
  caution: { label: "Needs Caution", variant: "warning" },
};

function getLoopModeGuidance(
  loopMode: SessionLoopMode,
  shadowRollup: SessionShadowRollup | null,
): { tone: "neutral" | "warning" | "success"; title: string; body: string } | null {
  if (!shadowRollup) return null;

  if (shadowRollup.readiness === "no_signal") {
    return {
      tone: "neutral",
      title: "No shadow signal yet",
      body: "Leave this session on Manual until Oikos has seen a few comparable decision points.",
    };
  }

  if (shadowRollup.readiness === "early") {
    return {
      tone: "neutral",
      title: "Shadow signal is still early",
      body: "Assist is reasonable for summaries and nudges, but keep Autopilot off until more wakeups match the shadow review.",
    };
  }

  if (shadowRollup.readiness === "caution") {
    return {
      tone: "warning",
      title: "Recent wakeups need caution",
      body: "Oikos recently diverged or acted more aggressively than the shadow ceiling. Stay on Manual or Assist until the signal cleans up.",
    };
  }

  if (loopMode === "manual") {
    return {
      tone: "success",
      title: "Promising Assist candidate",
      body: "Recent shadow reviews are lining up. This session looks safe to try in Assist before you consider bounded Autopilot.",
    };
  }

  if (loopMode === "assist") {
    return {
      tone: "success",
      title: "Promising Autopilot candidate",
      body: "Assist looks stable so far. This session is a good bounded Autopilot candidate for obvious continue turns.",
    };
  }

  return {
    tone: "success",
    title: "Autopilot signal looks healthy",
    body: "Shadow reviews are matching so far. Keep watching the alignment strip as this session keeps running.",
  };
}

function formatRecommendedAction(value: string | null): string | null {
  if (!value) return null;
  return value
    .split("_")
    .map((part) => (part ? part[0].toUpperCase() + part.slice(1) : part))
    .join(" ");
}

function formatOutcome(value: string | null): string | null {
  return formatRecommendedAction(value);
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
  latestShadowReview = null,
  shadowRollup = null,
  shadowReviewLoading = false,
  shadowReviewUnavailable = false,
}: SessionContextPaneProps) {
  const turnCount = session.user_messages + session.assistant_messages;
  const shadowState = latestShadowReview
    ? EXECUTION_STATE_META[latestShadowReview.executionState] ?? {
        label: latestShadowReview.executionState,
        variant: "neutral" as const,
      }
    : null;
  const shadowAlignment = latestShadowReview?.alignment
    ? SHADOW_ALIGNMENT_META[latestShadowReview.alignment] ?? {
        label: latestShadowReview.alignment,
        variant: "neutral" as const,
      }
    : null;
  const recommendedAction = formatRecommendedAction(latestShadowReview?.recommendedAction ?? null);
  const actualOutcome = formatOutcome(latestShadowReview?.actualOutcome ?? null);
  const expectedOutcome = formatOutcome(latestShadowReview?.expectedOutcome ?? null);
  const readinessMeta = shadowRollup
    ? SHADOW_READINESS_META[shadowRollup.readiness] ?? {
        label: shadowRollup.readiness,
        variant: "neutral" as const,
      }
    : null;
  const cautionCount = shadowRollup
    ? shadowRollup.moreAggressive + shadowRollup.different + shadowRollup.failed
    : 0;
  const loopModeGuidance = getLoopModeGuidance(session.loop_mode, shadowRollup);

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
          Stored session preference for Oikos supervision. Live autonomy remains shadow-only for now.
        </div>
        {loopModeGuidance ? (
          <div className={`session-loop-mode__advisory session-loop-mode__advisory--${loopModeGuidance.tone}`}>
            <div className="session-loop-mode__advisory-title">{loopModeGuidance.title}</div>
            <div className="session-loop-mode__advisory-body">{loopModeGuidance.body}</div>
          </div>
        ) : null}
      </div>

      <div className="session-pane-section">
        <div className="session-pane-section-title">Shadow Review</div>
        {shadowReviewLoading ? (
          <div className="session-shadow-review__empty">Loading latest shadow review...</div>
        ) : latestShadowReview ? (
          <div className="session-shadow-review" data-testid="session-shadow-review">
            {shadowRollup ? (
              <div className="session-shadow-review__rollup" data-testid="session-shadow-rollup">
                <div className="session-shadow-review__header">
                  {readinessMeta ? <Badge variant={readinessMeta.variant}>{readinessMeta.label}</Badge> : null}
                  <span className="session-shadow-review__stamp">
                    {shadowRollup.totalReviews} completed • {shadowRollup.pendingReviews} pending
                  </span>
                </div>
                <div className="session-shadow-review__meta">
                  Matched {shadowRollup.matched} • Conservative {shadowRollup.moreConservative} • Caution {cautionCount}
                </div>
              </div>
            ) : null}
            <div className="session-shadow-review__header">
              {shadowState ? <Badge variant={shadowState.variant}>{shadowState.label}</Badge> : null}
              {shadowAlignment ? (
                <Badge variant={shadowAlignment.variant}>{shadowAlignment.label}</Badge>
              ) : null}
              <span className="session-shadow-review__stamp">
                {formatFullDate(latestShadowReview.generatedAt)}
              </span>
            </div>
            <div className="session-shadow-review__summary">{latestShadowReview.summary}</div>
            {latestShadowReview.modeSummary ? (
              <div className="session-shadow-review__mode">{latestShadowReview.modeSummary}</div>
            ) : null}
            <div className="session-shadow-review__meta">
              Trigger: {latestShadowReview.triggerType}
            </div>
            {recommendedAction ? (
              <div className="session-shadow-review__meta">
                Recommended action: {recommendedAction}
              </div>
            ) : null}
            {expectedOutcome ? (
              <div className="session-shadow-review__meta">Shadow expected outcome: {expectedOutcome}</div>
            ) : null}
            {actualOutcome ? (
              <div className="session-shadow-review__meta">Actual outcome: {actualOutcome}</div>
            ) : null}
            {latestShadowReview.blockedReasons.length > 0 ? (
              <div className="session-shadow-review__blockers">
                {latestShadowReview.blockedReasons.map((reason) => (
                  <div key={reason} className="session-shadow-review__blocker">
                    {reason}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ) : (
          <div className="session-shadow-review__empty">
            {shadowReviewUnavailable
              ? "Shadow review is unavailable right now."
              : "No shadow review recorded for this session yet."}
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
