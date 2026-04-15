import { Badge, Button } from "../ui";
import type { AgentSession, SessionLoopMode } from "../../services/api/agents";
import { config } from "../../lib/config";
import { normalizeExecutionVenueLabel } from "../../lib/sessionExecutionHome";
import { resolveSessionRuntimeState } from "../../lib/sessionRuntime";
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
  const canDriveManagedLocalFromBrowser = interaction.liveControlAvailable;
  const turnCount = session.user_messages + session.assistant_messages;
  const runtime = resolveSessionRuntimeState(session);
  const homeLabel = normalizeExecutionVenueLabel(session.home_label);
  const runtimeBadgeVariant = runtime.isExecuting
    ? "success"
    : runtime.needsAttention
      ? "warning"
      : "neutral";
  const sessionControl = session.control ?? null;
  const attachCommand = interaction.hostReattachAvailable ? sessionControl?.attach_command?.trim() || null : null;
  const attachRunnerLabel = sessionControl?.source_runner_name?.trim() || "this machine";
  const loopModeCaption =
    config.demoMode
      ? "Preview only in the demo. Changes and live control are disabled."
      : !interaction.isManagedLocalSession
      ? "Stored session preference for what the assistant may do after each completed turn."
      : canDriveManagedLocalFromBrowser
        ? "Loop Mode changes review posture only. Keep driving the live session from Longhouse below or by reattaching on the host machine."
        : isManagedLocalCodex
          ? "For live Codex sessions, Loop Mode changes review posture only. Keep driving the live session from the host terminal."
          : "Loop Mode changes review posture only. Keep driving the live session from the host terminal.";
  const primaryActionDescription = interaction.canChatFromBrowser
    ? "Open the dock below and send the next prompt into the live session."
    : interaction.composerDisabledReason ?? interaction.notice?.body ?? interaction.capabilityDescription ?? undefined;

  return (
    <div className="session-context-pane">
      {/* Hero: title, provider, badges */}
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
          <span>{runtime.displayPhase}</span>
        </div>
        <div className="session-context-badges">
          <Badge variant={runtimeBadgeVariant}>{runtime.displayPhase}</Badge>
          <Badge variant={interaction.capabilityVariant} title={interaction.capabilityDescription ?? undefined}>
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
          {homeLabel ? <Badge variant="neutral">{homeLabel}</Badge> : null}
          {session.environment && session.environment !== "production" ? (
            <Badge variant="warning">{session.environment}</Badge>
          ) : null}
        </div>
        {interaction.capabilityDescription && (
          <div className="session-context-capability-summary" data-testid="session-capability-summary">
            {interaction.capabilityDescription}
          </div>
        )}
        <div
          className="session-context-managed-copy"
          data-testid="session-management-summary"
        >
          {interaction.managementDescription}
        </div>
        {interaction.managedLaunchSuggestion ? (
          <ManagedLaunchHintCard
            suggestion={interaction.managedLaunchSuggestion}
            testId="session-managed-launch-hint"
          />
        ) : null}
      </div>

      <div className="session-pane-section">
        <div className="session-pane-section-title">Next action</div>
        <div className="session-context-primary-action">
          <Button
            type="button"
            variant="primary"
            size="sm"
            onClick={onPrimaryAction}
            disabled={!interaction.canChatFromBrowser}
            title={!interaction.canChatFromBrowser ? primaryActionDescription : undefined}
          >
            {interaction.primaryActionLabel}
          </Button>
          <div className="session-context-primary-action-copy">
            {primaryActionDescription}
          </div>
        </div>
      </div>

      {/* Branch banner (not viewing head) */}
      {!isViewingHead && headThreadSession ? (
        <div
          className="session-pane-callout session-pane-callout--warning session-branch-banner"
          data-testid="session-branch-banner"
        >
          <div className="session-pane-callout-title">This is not the latest branch</div>
          <div className="session-pane-callout-copy">
            Latest head: {getSessionOriginLabel(headThreadSession)} from{" "}
            {formatContinuationStamp(headThreadSession.started_at)}.
          </div>
          <Button variant="secondary" size="sm" onClick={onOpenLatest}>
            Open Latest
          </Button>
        </div>
      ) : null}

      {/* Metadata */}
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

      {/* Reattach command */}
      {attachCommand ? (
        <div className="session-pane-section">
          <div className="session-pane-section-title">Reattach</div>
          <div className="session-pane-callout session-pane-callout--muted" data-testid="session-attach-callout">
            <div className="session-pane-callout-title">
              {isManagedLocalCodex ? "Reattach the live Codex terminal" : "Reattach on the host machine"}
            </div>
            <div className="session-pane-callout-copy">
              {isManagedLocalCodex
                ? canDriveManagedLocalFromBrowser
                  ? `This live Codex session is running on ${attachRunnerLabel}. Reopen the Codex TUI on the host machine anytime, or send prompts from Longhouse below.`
                  : `This live Codex session is running on ${attachRunnerLabel}. Use the host-machine command below to reopen the Codex TUI.`
                : canDriveManagedLocalFromBrowser
                  ? `This session is running on ${attachRunnerLabel}. Reattach on the host machine anytime, or keep sending prompts from Longhouse below.`
                  : `This session is running on ${attachRunnerLabel}. Use the host-machine command below to reopen the live terminal session.`}
            </div>
            <pre className="inspector-code-block" data-testid="session-attach-command">
              <code>{attachCommand}</code>
            </pre>
          </div>
        </div>
      ) : null}

      {/* Loop mode selector */}
      <LoopModeSelector
        currentMode={session.loop_mode}
        caption={loopModeCaption}
        pending={loopModePending}
        onChange={onLoopModeChange}
      />


      {/* Continuation notice */}
      {continuationNotice ? (
        <div
          className="session-pane-callout session-pane-callout--muted"
          data-testid="session-continuation-unavailable"
        >
          <div className="session-pane-callout-title">{continuationNotice.title}</div>
          <div className="session-pane-callout-copy">{continuationNotice.body}</div>
        </div>
      ) : null}

      {/* Summary */}
      {session.summary ? (
        <div className="session-pane-section">
          <div className="session-pane-section-title">Summary</div>
          <div className="session-context-summary">{session.summary}</div>
        </div>
      ) : null}

      {/* Continuations list */}
      <ContinuationsList
        sessions={threadSessions}
        currentSessionId={session.id}
        headSessionId={headThreadSession?.id ?? null}
        onOpenSession={onOpenSession}
      />
    </div>
  );
}
