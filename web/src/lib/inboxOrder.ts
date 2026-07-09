/**
 * Inbox order persistence (per-browser, localStorage).
 *
 * Three axes:
 *   - shelfOrder: ordered list of shelf session thread_ids.
 *   - repoOrder: ordered list of repo names. Repos not in the list slot to
 *     the top of their tier in default order (most-recent-session-start desc).
 *   - sessionOrder: per-repo ordered list of session thread_ids. Same rule —
 *     unknown sessions slot to the top in start-time-desc default order.
 *
 * Storage key: "longhouse:inbox-order:v1". shelfOrder is additive — missing
 * keys default to [] so existing repo/session drag order survives.
 */

const STORAGE_KEY = "longhouse:inbox-order:v1";

export interface InboxOrderState {
  shelfOrder: string[];
  repoOrder: string[];
  sessionOrder: Record<string, string[]>;
}

const empty: InboxOrderState = { shelfOrder: [], repoOrder: [], sessionOrder: {} };

export function readInboxOrder(): InboxOrderState {
  if (typeof window === "undefined") return empty;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return empty;
    const parsed = JSON.parse(raw) as Partial<InboxOrderState>;
    return {
      shelfOrder: Array.isArray(parsed.shelfOrder)
        ? parsed.shelfOrder.filter((s) => typeof s === "string")
        : [],
      repoOrder: Array.isArray(parsed.repoOrder)
        ? parsed.repoOrder.filter((s) => typeof s === "string")
        : [],
      sessionOrder: sanitizeSessionOrder(parsed.sessionOrder),
    };
  } catch {
    return empty;
  }
}

function sanitizeSessionOrder(value: unknown): Record<string, string[]> {
  if (!value || typeof value !== "object") return {};
  const out: Record<string, string[]> = {};
  for (const [repo, ids] of Object.entries(value as Record<string, unknown>)) {
    if (!Array.isArray(ids)) continue;
    out[repo] = ids.filter((s): s is string => typeof s === "string");
  }
  return out;
}

export function writeInboxOrder(state: InboxOrderState): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // ignore quota / private-mode errors
  }
}

/** Reorder a list by moving `from` to `to`. Pure. */
export function moveItem<T>(list: T[], from: number, to: number): T[] {
  if (from === to || from < 0 || from >= list.length) return list;
  const next = list.slice();
  const [item] = next.splice(from, 1);
  const insertAt = Math.max(0, Math.min(next.length, to));
  next.splice(insertAt, 0, item);
  return next;
}

/**
 * Apply a stored override to a default-ordered list of keys.
 * Keys present in `override` keep that order. Keys missing from `override`
 * stay in their default position relative to each other.
 *
 * Example:
 *   defaultOrder = [a, b, c, d]
 *   override     = [c, a]
 *   result       = [c, a, b, d]
 */
export function applyOrder<T>(defaultOrder: T[], override: T[]): T[] {
  if (override.length === 0) return defaultOrder;
  const overrideSet = new Set(override);
  const remaining = defaultOrder.filter((k) => !overrideSet.has(k));
  const overridden = override.filter((k) => defaultOrder.includes(k));
  return [...overridden, ...remaining];
}
