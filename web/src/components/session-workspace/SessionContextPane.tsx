import { Badge, Button } from "../ui";
import type { AgentSession, SessionLoopMode } from "../../services/api/agents";
import { config } from "../../lib/config";
import { normalizeExecutionVenueLabel } from "../../lib/sessionExecutionHome";
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
import { LoopModeSelector } from "./LoopModeSelector";
import { ContinuationsList } from "./ContinuationsList";
import { ManagedLaunchHintCard } from "./ManagedLaunchHintCard";
import { SessionRuntimeStrip } from "./SessionRuntimeStrip";

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
  const showAttachRecovery =
    Boolean(attachCommand) && !interaction.liveControlAvailable;
  const showAttachDebug =
    Boolean(attachCommand) && interaction.liveControlAvailable;
  const attachRunnerLabel =
    sessionControl?.source_runner_name?.trim() ||
    homeLabel ||
    interaction.sourceOriginLabel ||
    "this machine";
  const loopModeCaption = config.demoMode
    ? "Preview only in the demo."
    : !interaction.isManagedLocalSession
      ? "Stored preference only."
      : interaction.liveControlAvailable
        ? "Review posture only."
        : "Stored here. Applies when Longhouse regains control.";
  const attachRecoveryCopy = `Longhouse can still see this session here, but browser control is unavailable. Run this on ${attachRunnerLabel} to continue from the terminal.`;
  const attachDebugCopy = `Optional recovery command. Run this on ${attachRunnerLabel} to open a terminal UI for this existing managed ${interaction.providerLabel} session.`;
  const shouldShowNotice =
    continuationNotice && !interaction.managedLaunchSuggestion;
  const showControlSection =
    interaction.isManagedLocalSession ||
    showAttachRecovery ||
    shouldShowNotice ||
    interaction.managedLaunchSuggestion;

  const durationStr = formatDuration(session.started_at, session.ended_at);
  const statsLine = [
    `${turnCount} turns`,
    `${session.tool_calls} tools`,
    durationStr,
  ].join(" \u00b7 ");

  return (
    <div className="session-context-pane">
      {/* Zone 1 — Identity + live status */}
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
        <SessionRuntimeStrip
          session={session}
          interaction={interaction}
          hostLabel={attachRunnerLabel}
          variant="block"
          testId="session-sidebar-runtime"
        />
        <div className="session-context-stats" data-testid="session-stats-line">
          {statsLine}
        </div>
      </div>

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

      {/* Zone 2 — Actions */}
      {showControlSection ? (
        <div className="session-pane-section session-pane-section--actions">
          {showAttachRecovery ? (
            <details
              className="session-pane-disclosure"
              data-testid="session-attach-callout"
            >
              <summary className="session-pane-disclosure__summary">
                <span className="session-pane-disclosure__title">
                  Continue from host terminal
                </span>
                <span className="session-pane-disclosure__meta">
                  {attachRunnerLabel}
                </span>
              </summary>
              <div className="session-pane-disclosure__body">
                <div className="session-pane-disclosure__copy">
                  {attachRecoveryCopy}
                </div>
                <pre
                  className="inspector-code-block"
                  data-testid="session-attach-command"
                >
                  <code>{attachCommand}</code>
                </pre>
              </div>
            </details>
          ) : null}
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
          {interaction.isManagedLocalSession ? (
            <LoopModeSelector
              currentMode={session.loop_mode}
              caption={loopModeCaption}
              pending={loopModePending}
              onChange={onLoopModeChange}
            />
          ) : null}
        </div>
      ) : interaction.isManagedLocalSession ? (
        <div className="session-pane-section session-pane-section--actions">
          <LoopModeSelector
            currentMode={session.loop_mode}
            caption={loopModeCaption}
            pending={loopModePending}
            onChange={onLoopModeChange}
          />
        </div>
      ) : null}

      {showAttachDebug ? (
        <details
          className="session-pane-disclosure"
          data-testid="session-debug-attach"
        >
          <summary className="session-pane-disclosure__summary">
            <span className="session-pane-disclosure__title">Debug</span>
            <span className="session-pane-disclosure__meta">
              Terminal attach
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

      {/* Zone 3 — Metadata (collapsed) */}
      <details className="session-pane-disclosure">
        <summary className="session-pane-disclosure__summary">
          <span className="session-pane-disclosure__title">Details</span>
          <span className="session-pane-disclosure__meta">
            {session.git_branch || session.project || ""}
          </span>
        </summary>
        <div className="session-pane-disclosure__body">
          <div className="session-context-meta">
            <MetaRow
              label="Started"
              value={formatFullDate(session.started_at)}
            />
            <MetaRow label="Duration" value={durationStr} />
            {session.git_branch ? (
              <MetaRow label="Branch" value={session.git_branch} />
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

      {session.summary ? (
        <details className="session-pane-disclosure">
          <summary className="session-pane-disclosure__summary">
            <span className="session-pane-disclosure__title">
              Session summary
            </span>
          </summary>
          <div className="session-pane-disclosure__body">
            <div className="session-context-summary">{session.summary}</div>
          </div>
        </details>
      ) : null}

      <ContinuationsList
        sessions={threadSessions}
        currentSessionId={session.id}
        headSessionId={headThreadSession?.id ?? null}
        onOpenSession={onOpenSession}
      />
    </div>
  );
}
