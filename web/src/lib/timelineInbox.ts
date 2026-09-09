import { type TimelineSessionCard } from "../services/api/agents";
import { getProjectLabel } from "./sessionUtils";
import { isSessionClosed } from "./sessionRuntime";
import { applyOrder, type InboxOrderState } from "./inboxOrder";

export type InboxGroupKind = "project" | "automation";

export interface InboxRepoGroup {
  /** Stable raw project key used for persisted drag ordering. */
  repo: string;
  sessions: TimelineSessionCard[];
  /** User-facing label; intentionally separate from the raw project key. */
  label: string;
  /** Optional explanation for non-project groups. */
  description: string | null;
  kind: InboxGroupKind;
}

export interface InboxLayout {
  shelf: TimelineSessionCard[];
  unread: TimelineSessionCard[];
  /** Visible history: all non-shelf sessions, grouped by project. */
  history: InboxRepoGroup[];
  historyCount: number;
  shelfCount?: number;
}


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
  return isSessionClosed(session);
}

function historySortKey(card: TimelineSessionCard): number {
  return isCardClosed(card) ? closedAtMs(card) : startedAtMs(card);
}

function isAutomationSession(session: TimelineSessionCard["head"]): boolean {
  const actor = session.launch_actor?.trim().toLowerCase();
  const origin = session.origin_kind?.trim().toLowerCase();
  const cwd = session.cwd?.replace(/\/+$/, "").split("/").pop()?.toLowerCase();
  return (
    session.project === "agent-sessions" ||
    cwd === "agent-sessions" ||
    actor === "automation" ||
    origin?.includes("automation") === true
  );
}

export function getInboxGroupPresentation(
  repo: string,
  sessions: readonly TimelineSessionCard[],
): Pick<InboxRepoGroup, "label" | "description" | "kind"> {
  const automation = repo === "agent-sessions" || (
    sessions.length > 0 && sessions.every((session) => isAutomationSession(session.head))
  );
  if (automation) {
    return {
      label: "Automation runs",
      description: "Background OpenCode sessions · search only",
      kind: "automation",
    };
  }
  return {
    label: repo,
    description: null,
    kind: "project",
  };
}

function addToRepoMap(
  groups: Map<string, TimelineSessionCard[]>,
  repo: string,
  card: TimelineSessionCard,
): void {
  const list = groups.get(repo);
  if (list) list.push(card);
  else groups.set(repo, [card]);
}

/**
 * Is this card part of what the user is currently carrying?
 *
 * Reads the server's `working_set` tier rather than deciding locally. The
 * previous local predicate promoted a card when any control action was
 * available, or when it merely started within 24h. Both are the same mistake:
 * they rank on capability and age instead of on evidence the session is live.
 * Control grants never expire while terminals close constantly, so the shelf
 * grew without bound — 51 of 60 rows on a dogfood instance.
 *
 * `nowMs` is retained for signature stability with callers and tests; the
 * decision is no longer time-based.
 */
export function isOnShelf(card: TimelineSessionCard, _nowMs?: number): boolean {
  if (isCardClosed(card)) return false;
  return card.head?.session_state.working_set === "open";
}

/**
 * Is this card carrying an unacknowledged Console result?
 *
 * Server-derived (session_state.unread): a Console turn settled and no human
 * has opened the session since. Unread is an overlay on the tiers, not a tier —
 * a running unread session stays on the shelf; everything else moves into the
 * unread band until read-on-open clears it. See
 * control-plane/docs/specs/console-unread-acknowledgement.md.
 */
export function isUnread(card: TimelineSessionCard): boolean {
  return card.head?.session_state.unread === true;
}

/** Completion time of the unread result, for band sort and the row label. */
export function unreadResultAtMs(card: TimelineSessionCard): number {
  return parseMs(card.head?.session_state.last_result_at);
}

/**
 * Build the visible inbox layout:
 *   - Shelf: flat list of sessions the server says are open now.
 *   - Unread: a result-attention overlay between live work and history.
 *   - History: every other session, grouped by project.
 *
 * Every non-shelf, non-unread session enters History; liveness only controls
 * the row's closed styling and its ordering key.
 */
export function buildInboxLayout(
  cards: TimelineSessionCard[],
  order?: InboxOrderState,
  nowMs?: number,
): InboxLayout {
  const now = nowMs ?? Date.now();

  const shelfCards: TimelineSessionCard[] = [];
  const unreadCards: TimelineSessionCard[] = [];
  const historyByRepo = new Map<string, TimelineSessionCard[]>();

  for (const card of cards) {
    // The unread band carves the card out of the history bucket — never
    // duplicates it. A running unread session stays on the shelf; its band
    // membership re-derives when the new turn settles.
    if (isOnShelf(card, now)) {
      shelfCards.push(card);
    } else if (isUnread(card)) {
      unreadCards.push(card);
    } else {
      addToRepoMap(historyByRepo, getProjectLabel(card.head), card);
    }
  }

  shelfCards.sort((a, b) => startedAtMs(b) - startedAtMs(a));
  // The result that just landed goes on top (matches the closed-sort rationale).
  unreadCards.sort((a, b) => unreadResultAtMs(b) - unreadResultAtMs(a));

  const makeGroup = (repo: string, sessions: TimelineSessionCard[]): InboxRepoGroup => ({
    repo,
    sessions,
    ...getInboxGroupPresentation(repo, sessions),
  });

  const toGroups = (
    byRepo: Map<string, TimelineSessionCard[]>,
    sortKey: (card: TimelineSessionCard) => number,
    groupPriority?: (group: InboxRepoGroup) => number,
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
        groups.push(makeGroup(repo, reordered));
      } else {
        groups.push(makeGroup(repo, sessions));
      }
    }
    groups.sort((a, b) => {
      const aPriority = groupPriority?.(a) ?? 0;
      const bPriority = groupPriority?.(b) ?? 0;
      if (aPriority !== bPriority) return aPriority - bPriority;
      const aTop = sortKey(a.sessions[0]);
      const bTop = sortKey(b.sessions[0]);
      if (aTop !== bTop) return bTop - aTop;
      return a.repo.localeCompare(b.repo);
    });
    if (order?.repoOrder?.length) {
      const defaultRepos = groups.filter((group) => group.kind !== "automation").map((g) => g.repo);
      const orderedRepos = applyOrder(defaultRepos, order.repoOrder);
      const byRepoName = new Map(groups.map((g) => [g.repo, g]));
      const orderedProjects = orderedRepos
        .map((r) => byRepoName.get(r))
        .filter((g): g is InboxRepoGroup => g != null && g.kind !== "automation");
      const automationGroups = groups.filter((group) => group.kind === "automation");
      return [...orderedProjects, ...automationGroups];
    }
    return groups;
  };

  const history = toGroups(historyByRepo, historySortKey, (group) => group.kind === "automation" ? 1 : 0);
  const historyCount = history.reduce((n, g) => n + g.sessions.length, 0);

  const shelfOrdered = applyShelfOrder(shelfCards, order?.shelfOrder);

  return {
    shelf: shelfOrdered,
    unread: unreadCards,
    history,
    historyCount,
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
