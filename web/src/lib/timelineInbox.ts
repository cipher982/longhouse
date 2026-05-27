import { type TimelineSessionCard } from "../services/api/agents";
import { getProjectLabel } from "./sessionUtils";
import { isSessionClosed } from "./sessionRuntime";
import { applyOrder, type InboxOrderState } from "./inboxOrder";

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
  const started = card.root?.started_at || card.head?.started_at;
  if (!started) return 0;
  const ms = Date.parse(started);
  return Number.isFinite(ms) ? ms : 0;
}

function isCardClosed(card: TimelineSessionCard): boolean {
  const session = card.head;
  const status = session?.timeline_card?.status;
  if (status?.tone === "closed" || status?.label === "Closed") return true;
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
 *
 * Optional `order` override applies user-driven reordering on top of the
 * default sort. Repo names / session ids absent from the override keep
 * their default-relative position. The override is shared across both
 * tiers — a repo that lives in both Active and Closed gets the same slot
 * within each.
 */
export function buildInboxLayout(
  cards: TimelineSessionCard[],
  order?: InboxOrderState,
): InboxLayout {
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
      if (order?.sessionOrder?.[repo]?.length) {
        const defaultIds = sessions.map((s) => s.thread_id);
        const orderedIds = applyOrder(defaultIds, order.sessionOrder[repo]);
        const byId = new Map(sessions.map((s) => [s.thread_id, s]));
        const reordered = orderedIds
          .map((id) => byId.get(id))
          .filter((s): s is TimelineSessionCard => s != null);
        groups.push({ repo, sessions: reordered });
      } else {
        groups.push({ repo, sessions });
      }
    }
    groups.sort((a, b) => {
      const aTop = startedAtMs(a.sessions[0]);
      const bTop = startedAtMs(b.sessions[0]);
      if (aTop !== bTop) return bTop - aTop;
      return a.repo.localeCompare(b.repo);
    });
    if (order?.repoOrder?.length) {
      const defaultRepos = groups.map((g) => g.repo);
      const orderedRepos = applyOrder(defaultRepos, order.repoOrder);
      const byRepoName = new Map(groups.map((g) => [g.repo, g]));
      return orderedRepos
        .map((r) => byRepoName.get(r))
        .filter((g): g is InboxRepoGroup => g != null);
    }
    return groups;
  };

  const active = toGroups(activeByRepo);
  const closed = toGroups(closedByRepo);
  const closedCount = closed.reduce((n, g) => n + g.sessions.length, 0);

  return { active, closed, closedCount };
}
