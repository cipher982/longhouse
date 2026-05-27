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
  getProviderColor,
  getSessionOriginLabel,
  truncatePath,
} from "../../lib/sessionWorkspace";
import { ContinuationsList } from "./ContinuationsList";
import { ManagedLaunchHintCard } from "./ManagedLaunchHintCard";

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
  const interaction = getSessionInteractionCapabilities({
    session,
    isViewingHead,
    headThreadSession,
  });
  const turnCount = session.user_messages + session.assistant_messages;
  const homeLabel = normalizeExecutionVenueLabel(session.home_label);
  const sessionControl = session.control ?? null;
  const attachCommand = interaction.hostReattachAvailable
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
  const showStateSection =
    shouldShowNotice || interaction.managedLaunchSuggestion;

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
              <span
                className="session-context-provider-dot"
                style={{ backgroundColor: getProviderColor(session.provider) }}
              />
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
            <span
              className="session-context-provider-dot"
              style={{ backgroundColor: getProviderColor(session.provider) }}
            />
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
