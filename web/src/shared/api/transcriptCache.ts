/**
 * On-device transcript history (session-view-terminal-parity C5).
 *
 * Keeps the last transcript pages of recently viewed sessions in IndexedDB so
 * a reload or restart paints a session from disk while the network answers.
 * The cache is instant paint only: whatever it seeds is marked stale, so
 * opening a session still does the durable refresh
 * (session-viewport-freshness-epic.md). It never decides a session is fresh.
 *
 * Records are scoped to the Runtime Host origin plus the signed-in user, so
 * two accounts on one browser never read each other's transcripts. Signing
 * out wipes everything; a different user signing in wipes the other scopes.
 *
 * Every call degrades to "nothing cached" when IndexedDB is missing or throws
 * (private windows, blocked storage, quota): the app then reads the network
 * exactly as it did before this cache existed.
 */

/**
 * Budget. A lite transcript measured 1.71 MB raw for 1,306 events across nine
 * real sessions (spec C3), about 1.3 KB per event on the wire; the hydrated
 * objects stored here carry every default field, call it ~1.7 KB per event.
 * A session keeps its tail page plus at most PAGES_PER_SESSION pages of 200
 * events: 4 × 200 × 1.7 KB ≈ 1.4 MB at most, usually far less (most sessions
 * hold one page). 50 sessions × ~0.8 MB typical ≈ 40 MB, so the byte cap below
 * binds before the count cap only when many long sessions were scrolled
 * through, which is the case it exists for.
 */
export const MAX_CACHED_SESSIONS = 50;
export const MAX_CACHED_BYTES = 40 * 1024 * 1024;
export const PAGES_PER_SESSION = 4;
/** Opened tool bodies kept per session (each is one expanded row). */
export const BODIES_PER_SESSION = 40;

export type TranscriptRecordKind = "workspace" | "pages" | "bodies";

type SessionMeta = {
  sessionId: string;
  bytes: number;
  accessedAt: number;
  kinds: TranscriptRecordKind[];
  sizes?: Partial<Record<TranscriptRecordKind, number>>;
};

/** The few key/value operations the cache needs; IndexedDB in the browser. */
export interface TranscriptCacheBackend {
  get(key: string): Promise<unknown>;
  put(key: string, value: unknown): Promise<void>;
  delete(key: string): Promise<void>;
  keys(): Promise<string[]>;
}

const DB_NAME = "longhouse-transcripts";
const STORE = "records";
const SEP = "\u0000";

let backendPromise: Promise<TranscriptCacheBackend | null> | null = null;

function request<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error("IndexedDB request failed"));
  });
}

function openIndexedDbBackend(): Promise<TranscriptCacheBackend | null> {
  return new Promise((resolve) => {
    try {
      if (typeof indexedDB === "undefined") {
        resolve(null);
        return;
      }
      const open = indexedDB.open(DB_NAME, 1);
      open.onupgradeneeded = () => {
        open.result.createObjectStore(STORE);
      };
      open.onerror = () => resolve(null);
      open.onblocked = () => resolve(null);
      open.onsuccess = () => {
        const db = open.result;
        const store = (mode: IDBTransactionMode) => db.transaction(STORE, mode).objectStore(STORE);
        resolve({
          get: (key) => request(store("readonly").get(key)),
          put: async (key, value) => {
            await request(store("readwrite").put(value, key));
          },
          delete: async (key) => {
            await request(store("readwrite").delete(key));
          },
          keys: async () => (await request(store("readonly").getAllKeys())).map(String),
        });
      };
    } catch {
      resolve(null);
    }
  });
}

function backend(): Promise<TranscriptCacheBackend | null> {
  if (!backendPromise) backendPromise = openIndexedDbBackend();
  return backendPromise;
}

/** Tests swap in an in-memory backend, or null to model a browser without IndexedDB. */
export function setTranscriptCacheBackendForTests(next: TranscriptCacheBackend | null): void {
  backendPromise = Promise.resolve(next);
}

let currentScope: string | null = null;
/** Bumped by every scope change; a write that straddles one removes what it wrote. */
let scopeEpoch = 0;

/** Origin plus user id; null while signed out, which disables the cache. */
export function transcriptCacheScope(): string | null {
  return currentScope;
}

const metaKey = (scope: string, sessionId: string) => ["meta", scope, sessionId].join(SEP);
const dataKey = (scope: string, sessionId: string, kind: TranscriptRecordKind) =>
  ["data", scope, sessionId, kind].join(SEP);

async function safely<T>(fallback: T, work: (db: TranscriptCacheBackend) => Promise<T>): Promise<T> {
  try {
    const db = await backend();
    if (!db) return fallback;
    return await work(db);
  } catch {
    return fallback;
  }
}

function sizeOf(value: unknown): number {
  try {
    return JSON.stringify(value)?.length ?? 0;
  } catch {
    return 0;
  }
}

/**
 * Bind the cache to a signed-in user and drop every other scope's records: an
 * account switch must never leave the previous account's transcripts on disk.
 * Null (signed out, or auth still loading) only disables the cache; deleting
 * is wipeTranscriptCache's job, on an explicit sign-out.
 */
export async function setTranscriptCacheUser(userId: number | null, origin = globalThis.location?.origin ?? ""): Promise<void> {
  const next = userId == null ? null : `${origin}|${userId}`;
  if (next !== currentScope) scopeEpoch += 1;
  currentScope = next;
  const keep = currentScope;
  if (!keep) return;
  await deleteWhere((scope) => scope !== keep);
}

/** Sign-out: forget the scope and delete every cached transcript. */
export async function wipeTranscriptCache(): Promise<void> {
  currentScope = null;
  scopeEpoch += 1;
  await deleteWhere(() => true);
}

async function deleteWhere(matches: (scope: string) => boolean): Promise<void> {
  await safely(undefined, async (db) => {
    const keys = await db.keys();
    await Promise.all(keys.filter((key) => matches(key.split(SEP)[1] ?? "")).map((key) => db.delete(key)));
  });
}

export async function readCachedTranscript<T>(sessionId: string, kind: TranscriptRecordKind): Promise<T | null> {
  const scope = currentScope;
  if (!scope) return null;
  return safely<T | null>(null, async (db) => {
    const value = (await db.get(dataKey(scope, sessionId, kind))) as T | undefined;
    if (value === undefined) return null;
    const meta = (await db.get(metaKey(scope, sessionId))) as SessionMeta | undefined;
    if (meta) await db.put(metaKey(scope, sessionId), { ...meta, accessedAt: Date.now() });
    return value;
  });
}

/** The scope's sessions, most recently used first (for the startup paint). */
export async function listCachedSessions(): Promise<string[]> {
  const scope = currentScope;
  if (!scope) return [];
  return safely<string[]>([], async (db) => {
    const metas = await readMetas(db, scope);
    return metas.sort((a, b) => b.accessedAt - a.accessedAt).map((meta) => meta.sessionId);
  });
}

async function readMetas(db: TranscriptCacheBackend, scope: string): Promise<SessionMeta[]> {
  const prefix = ["meta", scope, ""].join(SEP);
  const keys = (await db.keys()).filter((key) => key.startsWith(prefix));
  const metas = await Promise.all(keys.map((key) => db.get(key) as Promise<SessionMeta | undefined>));
  return metas.filter((meta): meta is SessionMeta => Boolean(meta));
}

async function deleteSession(db: TranscriptCacheBackend, scope: string, meta: SessionMeta): Promise<void> {
  await Promise.all([
    ...meta.kinds.map((kind) => db.delete(dataKey(scope, meta.sessionId, kind))),
    db.delete(metaKey(scope, meta.sessionId)),
  ]);
}

export async function writeCachedTranscript(
  sessionId: string,
  kind: TranscriptRecordKind,
  value: unknown,
): Promise<void> {
  const scope = currentScope;
  if (!scope) return;
  const epoch = scopeEpoch;
  await safely(undefined, async (db) => {
    const key = metaKey(scope, sessionId);
    const previous = (await db.get(key)) as SessionMeta | undefined;
    const sizes: Partial<Record<TranscriptRecordKind, number>> = {
      ...(previous ? await kindSizes(db, scope, previous) : {}),
      [kind]: sizeOf(value),
    };
    const meta: SessionMeta = {
      sessionId,
      bytes: Object.values(sizes).reduce<number>((sum, size) => sum + (size ?? 0), 0),
      accessedAt: Date.now(),
      kinds: Array.from(new Set([...(previous?.kinds ?? []), kind])),
      sizes,
    };
    await db.put(dataKey(scope, sessionId, kind), value);
    await db.put(key, meta);
    if (scopeEpoch !== epoch) {
      // Signed out (or switched user) while this write was in flight: the
      // wipe may already have run, so take back what was just written.
      await deleteSession(db, scope, meta);
      return;
    }
    await evict(db, scope, sessionId);
  });
}

async function kindSizes(db: TranscriptCacheBackend, scope: string, meta: SessionMeta) {
  if (meta.sizes) return meta.sizes;
  const entries = await Promise.all(
    meta.kinds.map(async (kind) => [kind, sizeOf(await db.get(dataKey(scope, meta.sessionId, kind)))] as const),
  );
  return Object.fromEntries(entries);
}

/** Drop a session whose transcript generation moved on (branch, rehome). */
export async function forgetCachedSession(sessionId: string): Promise<void> {
  const scope = currentScope;
  if (!scope) return;
  await safely(undefined, async (db) => {
    const meta = (await db.get(metaKey(scope, sessionId))) as SessionMeta | undefined;
    if (meta) await deleteSession(db, scope, meta);
  });
}

/** Least recently used sessions go first, until both caps hold; the one just written stays. */
async function evict(db: TranscriptCacheBackend, scope: string, keepSessionId: string): Promise<void> {
  const metas = await readMetas(db, scope);
  let bytes = metas.reduce((sum, meta) => sum + meta.bytes, 0);
  let count = metas.length;
  const candidates = metas
    .filter((meta) => meta.sessionId !== keepSessionId)
    .sort((a, b) => a.accessedAt - b.accessedAt);
  for (const meta of candidates) {
    if (count <= MAX_CACHED_SESSIONS && bytes <= MAX_CACHED_BYTES) break;
    await deleteSession(db, scope, meta);
    bytes -= meta.bytes;
    count -= 1;
  }
}
