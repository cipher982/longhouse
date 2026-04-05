import { Badge, Button } from "../ui";
import type { AgentSession, SessionLoopMode } from "../../services/api/agents";
import { getExecutionHomeLabel } from "../../lib/sessionExecutionHome";
import { resolveSessionRuntimeState } from "../../lib/sessionRuntime";
import type { SessionTurnReview } from "../../services/api/oikos";
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
import { TurnReviewCard } from "./TurnReviewCard";
import { ContinuationsList } from "./ContinuationsList";

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
  latestTurnReview?: SessionTurnReview | null;
  turnReviewLoading?: boolean;
  turnReviewUnavailable?: boolean;
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
  latestTurnReview = null,
  turnReviewLoading = false,
  turnReviewUnavailable = false,
}: SessionContextPaneProps) {
  const interaction = getSessionInteractionCapabilities({
    session,
    isViewingHead,
    headThreadSession,
  });
  const isManagedLocalCodex = interaction.isManagedLocalCodex;
  const canDriveManagedLocalFromBrowser = interaction.canDriveManagedLocalSession;
  const turnCount = session.user_messages + session.assistant_messages;
  const runtime = resolveSessionRuntimeState(session);
  const executionHomeLabel =
    session.execution_home === "legacy" ? null : getExecutionHomeLabel(session.execution_home);
  const runtimeBadgeVariant = runtime.isExecuting
    ? "success"
    : runtime.needsAttention
      ? "warning"
      : "neutral";
  const attachCommand =
    session.execution_home === "managed_local" ? session.attach_command?.trim() || null : null;
  const managedLaunchProfile =
    session.execution_home === "managed_local" ? session.managed_launch_profile ?? null : null;
  const attachRunnerLabel = session.source_runner_name?.trim() || "this machine";
  const loopModeCaption =
    session.execution_home !== "managed_local"
      ? "Stored session preference for what Oikos may do after each completed assistant turn."
      : canDriveManagedLocalFromBrowser
        ? "Loop Mode changes review posture only. Keep driving the live session from Longhouse below or by reattaching on the host machine."
        : interaction.canChatFromBrowser
          ? "Loop Mode changes review posture only. Start or keep the cloud continuation from Longhouse below, or reattach on the host machine when available."
        : isManagedLocalCodex
          ? "For live Codex sessions, Loop Mode changes review posture only. Keep driving the live session from the host terminal."
          : "Loop Mode changes review posture only. Keep driving the live session from the host terminal.";
  const primaryActionDescription = interaction.canChatFromBrowser
    ? interaction.mode === "managed_local"
      ? "Open the dock below and send the next prompt into the live session."
      : interaction.mode === "head"
        ? "Open the dock below and continue this session in cloud from Longhouse."
        : interaction.mode === "promote"
          ? "Open the dock below and start a cloud continuation from this session."
          : interaction.mode === "branch"
            ? "Open the dock below and start a branched cloud continuation from this point."
            : "Open the dock below and continue from this session in the browser."
    : interaction.composerDisabledReason ?? interaction.notice?.body ?? interaction.capabilitySummary;

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
          <Badge variant={interaction.capabilityVariant}>{interaction.capabilityLabel}</Badge>
          <Badge variant="neutral">{turnCount} turns</Badge>
          <Badge variant="neutral">{session.tool_calls} tools</Badge>
          {executionHomeLabel ? <Badge variant="neutral">{executionHomeLabel}</Badge> : null}
          {session.environment && session.environment !== "production" ? (
            <Badge variant="warning">{session.environment}</Badge>
          ) : null}
        </div>
        <div className="session-context-capability-summary" data-testid="session-capability-summary">
          {interaction.capabilitySummary}
        </div>
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
                  : `This session is running on ${attachRunnerLabel}. Use the host-machine command below to reopen the live tmux session.`}
            </div>
            <pre className="inspector-code-block" data-testid="session-attach-command">
              <code>{attachCommand}</code>
            </pre>
          </div>
        </div>
      ) : null}

      {managedLaunchProfile ? (
        <div className="session-pane-section">
          <div className="session-pane-section-title">Launch profile</div>
          <div className="session-pane-callout session-pane-callout--muted" data-testid="session-launch-profile">
            <div className="session-pane-callout-title">Managed-local launcher contract</div>
            <div className="session-pane-callout-copy">
              Longhouse stored the redacted launch argv and allowlisted env exports for this live session.
            </div>
            <div className="session-context-meta">
              <MetaRow label="Required commands" value={managedLaunchProfile.required_commands.join(", ") || "None"} />
              <MetaRow label="Exported env keys" value={managedLaunchProfile.exported_env_keys.join(", ") || "None"} />
            </div>
            <pre className="inspector-code-block" data-testid="session-launch-profile-argv">
              <code>{managedLaunchProfile.argv.join(" ")}</code>
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

      {/* Turn review */}
      <TurnReviewCard
        review={latestTurnReview}
        loading={turnReviewLoading}
        unavailable={turnReviewUnavailable}
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
