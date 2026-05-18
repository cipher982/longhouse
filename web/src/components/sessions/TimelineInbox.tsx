/**
 * TimelineInbox — inbox-style timeline render.
 *
 * Two tiers (Active, then Closed), each grouped by repo. Repo order =
 * newest session start desc. Sessions inside a repo = start time desc.
 * Layout is anchored to start time so live runtime updates never reflow
 * the page. See lib/timelineInbox.ts for the pure layout function.
 */

import { useMemo } from "react";
import { type TimelineSessionCard } from "../../services/api/agents";
import { buildInboxLayout, type InboxRepoGroup } from "../../lib/timelineInbox";
import { SessionRow } from "./SessionRow";

export interface TimelineInboxProps {
  sessions: TimelineSessionCard[];
  onSessionClick: (thread: TimelineSessionCard) => void;
  onSessionPrefetch?: (thread: TimelineSessionCard) => void;
  allowHoverPrefetch?: () => boolean;
  relativeNowMs: number;
  highlightQuery?: string;
}

export function TimelineInbox({
  sessions,
  onSessionClick,
  onSessionPrefetch,
  allowHoverPrefetch,
  relativeNowMs,
  highlightQuery,
}: TimelineInboxProps) {
  const layout = useMemo(() => buildInboxLayout(sessions), [sessions]);

  if (layout.active.length === 0 && layout.closed.length === 0) {
    return null;
  }

  return (
    <div className="inbox" data-testid="timeline-inbox">
      {layout.active.length > 0 ? (
        <div className="inbox-section inbox-section--active">
          {layout.active.map((group) => (
            <RepoBlock
              key={`active:${group.repo}`}
              group={group}
              tier="active"
              onSessionClick={onSessionClick}
              onSessionPrefetch={onSessionPrefetch}
              allowHoverPrefetch={allowHoverPrefetch}
              relativeNowMs={relativeNowMs}
              highlightQuery={highlightQuery}
            />
          ))}
        </div>
      ) : null}

      {layout.closed.length > 0 ? (
        <div className="inbox-section inbox-section--closed">
          <div className="inbox-closed-divider" role="separator">
            <span className="inbox-closed-divider-label">Closed</span>
            <span className="inbox-closed-divider-count">{layout.closedCount}</span>
          </div>
          {layout.closed.map((group) => (
            <RepoBlock
              key={`closed:${group.repo}`}
              group={group}
              tier="closed"
              onSessionClick={onSessionClick}
              onSessionPrefetch={onSessionPrefetch}
              allowHoverPrefetch={allowHoverPrefetch}
              relativeNowMs={relativeNowMs}
              highlightQuery={highlightQuery}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

interface RepoBlockProps {
  group: InboxRepoGroup;
  tier: "active" | "closed";
  onSessionClick: (thread: TimelineSessionCard) => void;
  onSessionPrefetch?: (thread: TimelineSessionCard) => void;
  allowHoverPrefetch?: () => boolean;
  relativeNowMs: number;
  highlightQuery?: string;
}

function RepoBlock({
  group,
  tier,
  onSessionClick,
  onSessionPrefetch,
  allowHoverPrefetch,
  relativeNowMs,
  highlightQuery,
}: RepoBlockProps) {
  return (
    <section
      className="inbox-repo"
      data-tier={tier}
      data-repo={group.repo}
      aria-label={`${group.repo} sessions`}
    >
      <header className="inbox-repo-header">
        <h2 className="inbox-repo-name">{group.repo}</h2>
        <span className="inbox-repo-count">{group.sessions.length}</span>
      </header>
      <div className="inbox-repo-rows">
        {group.sessions.map((thread) => (
          <SessionRow
            key={thread.thread_id}
            thread={thread}
            onClick={() => onSessionClick(thread)}
            onPrefetch={onSessionPrefetch ? () => onSessionPrefetch(thread) : undefined}
            allowHoverPrefetch={allowHoverPrefetch}
            relativeNowMs={relativeNowMs}
            highlightQuery={highlightQuery}
            closed={tier === "closed"}
          />
        ))}
      </div>
    </section>
  );
}
