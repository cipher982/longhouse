import { type TimelineSessionCard } from "../services/api/agents";
import { getProjectLabel } from "./sessionUtils";
import { isSessionClosed } from "./sessionRuntime";
import { applyOrder, type InboxOrderState } from "./inboxOrder";

export interface InboxRepoGroup {
  repo: string;
  sessions: TimelineSessionCard[];
}

export interface InboxLayout {
  shelf: TimelineSessionCard[];
  active: InboxRepoGroup[];
  closed: InboxRepoGroup[];
  closedCount: number;
  shelfCount?: number;
}

export const SHELF_RECENCY_MS = 24 * 60 * 60 * 1000;

function parseMs(value: string | null | undefined): number {
  if (!value) return 0;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : 0;
}

export function startedAtMs(card: TimelineSessionCard): number {
  return parseMs(card.root?.started_at || card.head?.started_at);
}

/**
 * When a closed session last exited. Uses the head (latest run) close time so
 * a just-closed continuation floats up, falling back through last activity to
 * start time when `ended_at` is absent (e.g. inferred process_gone). This is
 * the same timestamp the card renders as "Closed Xh ago", so sort matches label.
 */
function closedAtMs(card: TimelineSessionCard): number {
  const head = card.head;
  return parseMs(head?.ended_at || head?.last_activity_at || head?.started_at) || startedAtMs(card);
}

function isCardClosed(card: TimelineSessionCard): boolean {
  const session = card.head;
  const status = session?.timeline_card?.status;
  if (status?.tone === "closed" || status?.label === "Closed") return true;
  return isSessionClosed(session);
}

export function isOnShelf(card: TimelineSessionCard, nowMs: number): boolean {
  if (isCardClosed(card)) return false;
  const caps = card.head?.capabilities;
  if (caps?.live_control_available || caps?.host_reattach_available) return true;
  return (nowMs - startedAtMs(card)) < SHELF_RECENCY_MS;
}

/**
 * Build the three-tier inbox layout:
 *   - Shelf: flat list of steerable or recent (<24h) open sessions,
 *     sorted by start time desc (frozen). Ordered by shelfOrder.
 *   - Active (archive): repo-grouped non-shelf open sessions,
 *     sorted by start time desc (frozen).
 *   - Closed: repo-grouped closed sessions, sorted by close time desc.
 *
 * Active ordering is intentionally anchored to start time so in-flight runtime
 * updates never reflow the page — that's what kills the timeline jitter when
 * several agents are churning. Closed sessions are terminal (no churn risk), so
 * we sort them by exit time instead: the thing you just stepped away from lands
 * on top.
 *
 * Optional `order` override applies user-driven reordering on top of the
 * default sort. Repo names / session ids absent from the override keep
 * their default-relative position.
 */
export function buildInboxLayout(
  cards: TimelineSessionCard[],
  order?: InboxOrderState,
  nowMs?: number,
): InboxLayout {
  const now = nowMs ?? Date.now();

  const shelfCards: TimelineSessionCard[] = [];
  const activeByRepo = new Map<string, TimelineSessionCard[]>();
  const closedByRepo = new Map<string, TimelineSessionCard[]>();

  for (const card of cards) {
    if (isCardClosed(card)) {
      const repo = getProjectLabel(card.head);
      const list = closedByRepo.get(repo);
      if (list) list.push(card);
      else closedByRepo.set(repo, [card]);
    } else if (isOnShelf(card, now)) {
      shelfCards.push(card);
    } else {
      const repo = getProjectLabel(card.head);
      const list = activeByRepo.get(repo);
      if (list) list.push(card);
      else activeByRepo.set(repo, [card]);
    }
  }

  shelfCards.sort((a, b) => startedAtMs(b) - startedAtMs(a));

  const toGroups = (
    byRepo: Map<string, TimelineSessionCard[]>,
    sortKey: (card: TimelineSessionCard) => number,
  ): InboxRepoGroup[] => {
    const groups: InboxRepoGroup[] = [];
    for (const [repo, sessions] of byRepo) {
      sessions.sort((a, b) => sortKey(b) - sortKey(a));
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
      const aTop = sortKey(a.sessions[0]);
      const bTop = sortKey(b.sessions[0]);
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

  const active = toGroups(activeByRepo, startedAtMs);
  const closed = toGroups(closedByRepo, closedAtMs);
  const closedCount = closed.reduce((n, g) => n + g.sessions.length, 0);

  const shelfOrdered = applyShelfOrder(shelfCards, order?.shelfOrder);

  return {
    shelf: shelfOrdered,
    active,
    closed,
    closedCount,
    shelfCount: shelfCards.length,
  };
}

function applyShelfOrder(
  cards: TimelineSessionCard[],
  shelfOrder?: string[],
): TimelineSessionCard[] {
  if (!shelfOrder?.length) return cards;
  const defaultIds = cards.map((s) => s.thread_id);
  const orderedIds = applyOrder(defaultIds, shelfOrder);
  const byId = new Map(cards.map((s) => [s.thread_id, s]));
  return orderedIds
    .map((id) => byId.get(id))
    .filter((s): s is TimelineSessionCard => s != null);
}
