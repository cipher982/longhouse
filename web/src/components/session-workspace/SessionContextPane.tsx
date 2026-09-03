import { Badge, Button } from "../ui";
import type { AgentSession } from "../../services/api/agents";
import { normalizeExecutionVenueLabel } from "../../lib/sessionExecutionHome";
import { isSessionClosed } from "../../lib/sessionRuntime";
import { getBranchLabel } from "../../lib/sessionUtils";
import {
  formatContinuationStamp,
  formatDuration,
  formatProviderLabel,
  formatFullDate,
  getSessionInteractionCapabilities,
  getSessionOriginLabel,
  truncatePath,
} from "../../lib/sessionWorkspace";
import { ContinuationsList } from "./ContinuationsList";
import { WorkflowRunsPanel } from "./WorkflowRunsPanel";
import { ManagedLaunchHintCard } from "./ManagedLaunchHintCard";
import { ProviderGlyph } from "../ProviderGlyph";
import { formatResumeReason } from "./ResumeSessionModal";

/** 501447 → "501k", 1_250_000 → "1.3M". */
function compactTokens(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}k`;
  return String(tokens);
}

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
  /** When true (drawer mode), suppress the title hero — the page header already shows it. */
  hideHero?: boolean;
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
  hideHero = false,
}: SessionContextPaneProps) {
  const interaction = getSessionInteractionCapabilities({ session });
  const turnCount = session.user_messages + session.assistant_messages;
  const homeLabel = normalizeExecutionVenueLabel(session.home_label);
  const sessionControl = session.control ?? null;
  // Reattach eligibility is projected from a durable connection row that never
  // consults host state, so it survives the machine going away. Offering the
  // attach command beside "the machine running this session is offline" tells
  // the user to run something on a machine Longhouse cannot reach.
  const hostReachable =
    session.session_state.host.state !== "offline" && session.session_state.host.state !== "stale";
  const attachCommand =
    interaction.hostReattachAvailable && hostReachable
      ? sessionControl?.attach_command?.trim() || null
      : null;
  const showAttachDebug = Boolean(attachCommand);
  const attachRunnerLabel =
    sessionControl?.source_runner_name?.trim() ||
    homeLabel ||
    interaction.sourceOriginLabel ||
    "this machine";
  const attachDebugCopy = `Run this on ${attachRunnerLabel} to open this existing managed ${interaction.providerLabel} session in a terminal UI. This does not restart the session.`;
  const shouldShowNotice =
    continuationNotice && !interaction.managedLaunchSuggestion;
  const resumeAction = session.session_state.control.actions.resume;
  // Gate on the run, not the disposition. Exiting a terminal ends the run but
  // never closes the session — `closed_at` is only written by an explicit user
  // close — so this callout was suppressed for exactly the sessions that needed
  // it, leaving an ended Helm session with no Resume button and no reason why.
  const showResumeUnavailable =
    isViewingHead &&
    session.session_state.mode === "helm" &&
    (session.session_state.disposition.state === "closed" ||
      session.session_state.run?.lifecycle === "ended") &&
    resumeAction.state !== "available";
  const showStateSection =
    shouldShowNotice || interaction.managedLaunchSuggestion || showResumeUnavailable;

  const durationStr = formatDuration(
    session.started_at,
    isSessionClosed(session) ? session.ended_at : null,
  );
  const toolCallLabel =
    session.tool_calls === 1 ? "1 tool call" : `${session.tool_calls} tool calls`;
  const statsLine = [
    `${turnCount} turns`,
    toolCallLabel,
    durationStr,
  ].join(" \u00b7 ");
  const branchLabel = getBranchLabel(session.git_branch);
  const metadataMeta = branchLabel || session.project || null;

  return (
    <div className="session-context-pane">
      {hideHero ? null : (
        <div className="session-pane-section session-pane-section--hero">
          <div className="session-context-title">{title}</div>
          <div className="session-context-subtitle">
            <span className="session-context-provider">
              <ProviderGlyph provider={session.provider} size={18} />
              {formatProviderLabel(session.provider)}
            </span>
            {homeLabel ? (
              <span className="session-context-subtitle__sep">{homeLabel}</span>
            ) : null}
            {session.environment && session.environment !== "production" ? (
              <Badge variant="warning" data-testid="session-env-badge">
                {session.environment}
              </Badge>
            ) : null}
          </div>
          <div className="session-context-stats" data-testid="session-stats-line">
            {statsLine}
          </div>
        </div>
      )}
      {hideHero ? (
        <div className="session-context-stats session-context-stats--drawer" data-testid="session-stats-line">
          <span className="session-context-provider">
            <ProviderGlyph provider={session.provider} size={18} />
            {formatProviderLabel(session.provider)}
          </span>
          {homeLabel ? <span className="session-context-subtitle__sep">{homeLabel}</span> : null}
          {session.environment && session.environment !== "production" ? (
            <Badge variant="warning" data-testid="session-env-badge">
              {session.environment}
            </Badge>
          ) : null}
          <span className="session-context-subtitle__sep">{statsLine}</span>
        </div>
      ) : null}

      {!isViewingHead && headThreadSession ? (
        <div
          className="session-pane-callout session-pane-callout--warning session-branch-banner"
          data-testid="session-branch-banner"
        >
          <div className="session-pane-callout-title">
            This is not the latest branch
          </div>
          <div className="session-pane-callout-copy">
            Latest head: {getSessionOriginLabel(headThreadSession)} from{" "}
            {formatContinuationStamp(headThreadSession.started_at)}.
          </div>
          <Button variant="secondary" size="sm" onClick={onOpenLatest}>
            Open Latest
          </Button>
        </div>
      ) : null}

      {showStateSection ? (
        <div className="session-pane-section session-pane-section--state">
          {interaction.managedLaunchSuggestion ? (
            <ManagedLaunchHintCard
              suggestion={interaction.managedLaunchSuggestion}
              testId="session-managed-launch-hint"
            />
          ) : null}
          {shouldShowNotice ? (
            <div
              className="session-pane-callout session-pane-callout--muted"
              data-testid="session-continuation-unavailable"
            >
              <div className="session-pane-callout-title">
                {continuationNotice.title}
              </div>
              <div className="session-pane-callout-copy">
                {continuationNotice.body}
              </div>
            </div>
          ) : null}
          {showResumeUnavailable ? (
            <div
              className="session-pane-callout session-pane-callout--muted"
              data-testid="session-resume-unavailable"
            >
              <div className="session-pane-callout-title">Resume unavailable</div>
              <div className="session-pane-callout-copy">
                {formatResumeReason(resumeAction.reason)}.
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {session.recap?.text ? (
        <div className="session-context-recap" data-testid="session-recap">
          <div className="session-context-recap__label">Recap</div>
          <div className="session-context-recap__text">{session.recap.text}</div>
        </div>
      ) : null}
      {session.summary ? (
        <details className="session-pane-disclosure session-pane-disclosure--tertiary session-pane-disclosure--summary">
          <summary className="session-pane-disclosure__summary">
            <span className="session-pane-disclosure__title">
              Summary
            </span>
            <span className="session-pane-disclosure__meta">Read-only</span>
          </summary>
          <div className="session-pane-disclosure__body">
            <div className="session-context-summary">{session.summary}</div>
          </div>
        </details>
      ) : null}

      <details className="session-pane-disclosure session-pane-disclosure--tertiary">
        <summary className="session-pane-disclosure__summary">
          <span className="session-pane-disclosure__title">Metadata</span>
          {metadataMeta ? (
            <span className="session-pane-disclosure__meta">
              {metadataMeta}
            </span>
          ) : null}
        </summary>
        <div className="session-pane-disclosure__body">
          <div className="session-context-meta">
            <MetaRow
              label="Started"
              value={formatFullDate(session.started_at)}
            />
            <MetaRow label="Duration" value={durationStr} />
            {session.usage_latest ? (
              <MetaRow
                label="Model"
                value={[
                  session.usage_latest.model,
                  session.usage_latest.effort,
                  `${compactTokens(session.usage_latest.context_tokens)} context`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              />
            ) : null}
            {branchLabel ? (
              <MetaRow label="Branch" value={branchLabel} />
            ) : null}
            {session.cwd ? (
              <MetaRow
                label="Workspace"
                value={truncatePath(session.cwd, 60)}
              />
            ) : null}
            {session.project ? (
              <MetaRow label="Project" value={session.project} />
            ) : null}
          </div>
        </div>
      </details>

      <ContinuationsList
        sessions={threadSessions}
        currentSessionId={session.id}
        headSessionId={headThreadSession?.id ?? null}
        onOpenSession={onOpenSession}
      />

      <WorkflowRunsPanel sessionId={session.id} />

      {showAttachDebug ? (
        <details
          className="session-pane-disclosure session-pane-disclosure--tertiary session-pane-disclosure--debug"
          data-testid="session-debug-attach"
        >
          <summary className="session-pane-disclosure__summary">
            <span className="session-pane-disclosure__title">Terminal</span>
            <span className="session-pane-disclosure__meta">
              Attach command
            </span>
          </summary>
          <div className="session-pane-disclosure__body">
            <div className="session-pane-disclosure__copy">
              {attachDebugCopy}
            </div>
            <pre
              className="inspector-code-block"
              data-testid="session-debug-attach-command"
            >
              <code>{attachCommand}</code>
            </pre>
          </div>
        </details>
      ) : null}
    </div>
  );
}
