import { type TimelineSessionCard, getTimelineCardAnchor } from "../services/api/agents";
import { getProjectLabel } from "./sessionUtils";
import { isSessionClosed } from "./sessionRuntime";

export interface InboxRepoGroup {
  repo: string;
  sessions: TimelineSessionCard[];
}

export interface InboxLayout {
  active: InboxRepoGroup[];
  closed: InboxRepoGroup[];
  closedCount: number;
}

function startedAtMs(card: TimelineSessionCard): number {
  const anchor = getTimelineCardAnchor(card);
  const started = anchor || card.root?.started_at || card.head?.started_at;
  if (!started) return 0;
  const ms = Date.parse(started);
  return Number.isFinite(ms) ? ms : 0;
}

function isCardClosed(card: TimelineSessionCard): boolean {
  const session = card.head;
  const status = session?.timeline_card?.status;
  if (status?.tone === "closed" || status?.label === "Closed") return true;
  const lifecycle = session?.runtime_facts?.lifecycle?.state;
  if (lifecycle === "closed") return true;
  if (lifecycle === "open") return false;
  return isSessionClosed(session);
}

/**
 * Build the two-tier inbox layout:
 *   - Active repos at the top, ordered by their newest session start (desc)
 *   - Closed repos below, same ordering rule
 *   - Sessions inside each repo always sort by start time desc (frozen).
 *
 * This is intentionally pure and stable: re-running it on the same input
 * produces the same output, regardless of in-flight runtime updates. That's
 * what kills the timeline jitter — order is anchored to start time, not to
 * "most-recently-touched".
 */
export function buildInboxLayout(cards: TimelineSessionCard[]): InboxLayout {
  const activeByRepo = new Map<string, TimelineSessionCard[]>();
  const closedByRepo = new Map<string, TimelineSessionCard[]>();

  for (const card of cards) {
    const repo = getProjectLabel(card.head);
    const bucket = isCardClosed(card) ? closedByRepo : activeByRepo;
    const list = bucket.get(repo);
    if (list) list.push(card);
    else bucket.set(repo, [card]);
  }

  const toGroups = (byRepo: Map<string, TimelineSessionCard[]>): InboxRepoGroup[] => {
    const groups: InboxRepoGroup[] = [];
    for (const [repo, sessions] of byRepo) {
      sessions.sort((a, b) => startedAtMs(b) - startedAtMs(a));
      groups.push({ repo, sessions });
    }
    groups.sort((a, b) => {
      const aTop = startedAtMs(a.sessions[0]);
      const bTop = startedAtMs(b.sessions[0]);
      if (aTop !== bTop) return bTop - aTop;
      return a.repo.localeCompare(b.repo);
    });
    return groups;
  };

  const active = toGroups(activeByRepo);
  const closed = toGroups(closedByRepo);
  const closedCount = closed.reduce((n, g) => n + g.sessions.length, 0);

  return { active, closed, closedCount };
}
