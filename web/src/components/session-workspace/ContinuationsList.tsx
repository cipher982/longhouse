import type { AgentSession } from "../../services/api/agents";
import { Badge } from "../ui";
import { formatContinuationStamp, getSessionOriginLabel } from "../../lib/sessionWorkspace";

interface ContinuationsListProps {
  sessions: AgentSession[];
  currentSessionId: string;
  headSessionId: string | null;
  onOpenSession: (sessionId: string) => void;
}

export function ContinuationsList({
  sessions,
  currentSessionId,
  headSessionId,
  onOpenSession,
}: ContinuationsListProps) {
  if (sessions.length <= 1) return null;

  return (
    <div
      className="session-pane-section session-pane-section--grow session-lineage-panel"
      data-testid="session-lineage-panel"
    >
      <div className="session-pane-section-title">Continuations</div>
      <div className="session-context-thread-list">
        {sessions.map((threadSession) => {
          const isCurrent = threadSession.id === currentSessionId;
          const isHead = threadSession.id === headSessionId;
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
  );
}
