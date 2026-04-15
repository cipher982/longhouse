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
  onPrimaryAction?: () => void;
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
  onPrimaryAction,
  continuationNotice = null,
  loopModePending = false,
  onLoopModeChange,
}: SessionContextPaneProps) {
  const interaction = getSessionInteractionCapabilities({
    session,
    isViewingHead,
    headThreadSession,
  });
  const isManagedLocalCodex = interaction.isManagedLocalCodex;
  const turnCount = session.user_messages + session.assistant_messages;
  const homeLabel = normalizeExecutionVenueLabel(session.home_label);
  const sessionControl = session.control ?? null;
  const attachCommand = interaction.hostReattachAvailable
    ? sessionControl?.attach_command?.trim() || null
    : null;
  const attachRunnerLabel =
    sessionControl?.source_runner_name?.trim() ||
    homeLabel ||
    interaction.sourceOriginLabel ||
    "this machine";
  const statusEyebrow = interaction.liveControlAvailable
    ? "Live session"
    : interaction.hostReattachAvailable
      ? "Managed session"
      : "Imported session";
  const statusSummary = interaction.liveControlAvailable
    ? `Live on ${attachRunnerLabel}. Send prompts from Longhouse or reattach on the host.`
    : interaction.hostReattachAvailable
      ? `Live on ${attachRunnerLabel}. This view stays synced, but continue from the host terminal.`
      : interaction.managedLaunchSuggestion
        ? `Archived here only. Start the next ${interaction.providerLabel} session through Longhouse when you need live control.`
        : "Archived here only.";
  const loopModeCaption = config.demoMode
    ? "Preview only in the demo."
    : !interaction.isManagedLocalSession
      ? "Stored preference only."
      : interaction.liveControlAvailable
        ? "Review posture only."
        : isManagedLocalCodex
          ? "Stored here. Applies when Longhouse regains control."
          : "Stored here. Applies when Longhouse regains control.";
  const attachDisclosureCopy = interaction.liveControlAvailable
    ? `Run this on ${attachRunnerLabel} to reopen the live terminal without breaking the Longhouse control path.`
    : `Run this on ${attachRunnerLabel} to reopen the live terminal session.`;
  const shouldShowNotice =
    continuationNotice && !interaction.managedLaunchSuggestion;
  const showControlSection =
    interaction.canChatFromBrowser ||
    attachCommand ||
    shouldShowNotice ||
    interaction.managedLaunchSuggestion;

  return (
    <div className="session-context-pane">
      <div className="session-pane-section session-pane-section--hero">
        <div className="session-pane-eyebrow">{statusEyebrow}</div>
        <div className="session-context-title">{title}</div>
        <div className="session-context-subtitle">
          <span className="session-context-provider">
            <span
              className="session-context-provider-dot"
              style={{ backgroundColor: getProviderColor(session.provider) }}
            />
            {formatProviderLabel(session.provider)}
          </span>
          {homeLabel ? <span>{homeLabel}</span> : null}
        </div>
        <div className="session-context-badges">
          <Badge
            variant={interaction.capabilityVariant}
            title={interaction.capabilityDescription ?? undefined}
          >
            {interaction.capabilityLabel}
          </Badge>
          <Badge
            variant={interaction.managementVariant}
            data-testid="session-management-badge"
            title={interaction.managementDescription}
          >
            {interaction.managementLabel}
          </Badge>
          <Badge variant="neutral">{turnCount} turns</Badge>
          <Badge variant="neutral">{session.tool_calls} tools</Badge>
          {session.environment && session.environment !== "production" ? (
            <Badge variant="warning">{session.environment}</Badge>
          ) : null}
        </div>
        <SessionRuntimeStrip
          session={session}
          interaction={interaction}
          hostLabel={attachRunnerLabel}
          variant="block"
          testId="session-sidebar-runtime"
        />
        <div
          className="session-context-managed-copy"
          data-testid="session-management-summary"
        >
          {statusSummary}
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

      {showControlSection ? (
        <div className="session-pane-section">
          <div className="session-pane-section-title">Control</div>
          {interaction.canChatFromBrowser && onPrimaryAction ? (
            <div className="session-context-primary-action">
              <Button
                type="button"
                variant="primary"
                size="sm"
                onClick={onPrimaryAction}
              >
                Focus composer
              </Button>
              <div className="session-context-primary-action-copy">
                Send the next prompt from the dock below.
              </div>
            </div>
          ) : null}
          {attachCommand ? (
            <details
              className="session-pane-disclosure"
              data-testid="session-attach-callout"
            >
              <summary className="session-pane-disclosure__summary">
                <span className="session-pane-disclosure__title">
                  {isManagedLocalCodex
                    ? "Reattach the live Codex terminal"
                    : "Reattach on the host machine"}
                </span>
                <span className="session-pane-disclosure__meta">
                  {attachRunnerLabel}
                </span>
              </summary>
              <div className="session-pane-disclosure__body">
                <div className="session-pane-disclosure__copy">
                  {attachDisclosureCopy}
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
        </div>
      ) : null}

      <div className="session-pane-section">
        <div className="session-pane-section-title">Details</div>
        <div className="session-context-meta">
          <MetaRow label="Started" value={formatFullDate(session.started_at)} />
          <MetaRow
            label="Duration"
            value={formatDuration(session.started_at, session.ended_at)}
          />
          {session.git_branch ? (
            <MetaRow label="Branch" value={session.git_branch} />
          ) : null}
          {session.cwd ? (
            <MetaRow label="Workspace" value={truncatePath(session.cwd, 60)} />
          ) : null}
          {session.project ? (
            <MetaRow label="Project" value={session.project} />
          ) : null}
        </div>
      </div>

      {interaction.isManagedLocalSession ? (
        <LoopModeSelector
          currentMode={session.loop_mode}
          caption={loopModeCaption}
          pending={loopModePending}
          onChange={onLoopModeChange}
        />
      ) : null}

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
