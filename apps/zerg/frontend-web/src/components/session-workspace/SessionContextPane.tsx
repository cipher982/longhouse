import { Badge, Button } from "../ui";
import type { AgentSession } from "../../services/api/agents";
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
}: SessionContextPaneProps) {
  const turnCount = session.user_messages + session.assistant_messages;

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
