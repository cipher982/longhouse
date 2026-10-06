/**
 * Wires the React Query cache to on-device transcript history
 * (`shared/api/transcriptCache.ts`, session-view-terminal-parity C5).
 *
 * - Writes: the session page's first workspace page, its paged transcript
 *   (newest PAGES_PER_SESSION pages) and any full tool bodies loaded, a moment
 *   after each lands from the network.
 * - Paint: when the session page (or the rail warming it) asks for a session
 *   the cache doesn't hold, the disk copy is seeded marked stale, so the
 *   page's own fetch still runs and replaces it. Disk beats a skeleton; the
 *   network beats disk.
 * - Older pages: once the session is open and its tail came from the network,
 *   the stored pages above it are stitched on with the same rules the live
 *   tail refresh uses (`stitchProjectionTail`), so scrolling up reads disk
 *   before the network. Another generation or head session drops them.
 */
import type { InfiniteData, Query, QueryClient } from "@tanstack/react-query";
import {
  agentSessionWorkspaceQueryOptions,
  stitchProjectionTail,
} from "@/shared/api/useAgentSessions";
import type {
  AgentSessionProjectionResponse,
  AgentSessionWorkspaceResponse,
  SessionEventBodiesResponse,
} from "@/shared/api/agents";
import {
  BODIES_PER_SESSION,
  PAGES_PER_SESSION,
  forgetCachedSession,
  listCachedSessions,
  readCachedTranscript,
  writeCachedTranscript,
} from "@/shared/api/transcriptCache";

/** The page size the session page and the rail both use (one cache entry). */
const PAGE_SIZE = 200;
/** How many sessions the startup paint seeds: the ones a ⌃-number reaches. */
export const STARTUP_PAINT_COUNT = 8;
const WRITE_DELAY_MS = 1_000;

type ProjectionPages = InfiniteData<AgentSessionProjectionResponse>;
type WorkspaceRecord = { workspace: AgentSessionWorkspaceResponse; savedAt: number };
type PagesRecord = { pages: ProjectionPages["pages"]; pageParams: ProjectionPages["pageParams"]; savedAt: number };
type BodiesRecord = { entries: { cursors: string[]; response: SessionEventBodiesResponse }[] };

const workspaceKey = (sessionId: string) =>
  agentSessionWorkspaceQueryOptions(sessionId, { limit: PAGE_SIZE }).queryKey;
const pagesKey = (sessionId: string) =>
  ["agent-session-projection-infinite", sessionId, { limit: PAGE_SIZE, branch_mode: "head" }] as const;

type Params = Record<string, unknown> | undefined;

/** Only the owner's own head-branch view at the shared page size is kept. */
function persistedKind(queryKey: readonly unknown[]): "workspace" | "pages" | "bodies" | null {
  const params = queryKey[2] as Params;
  if (typeof queryKey[1] !== "string") return null;
  if (queryKey[0] === "agent-session-workspace") {
    return params?.limit === PAGE_SIZE &&
      params?.branch_mode === "head" &&
      params?.shared_by == null &&
      params?.share_token == null
      ? "workspace"
      : null;
  }
  if (queryKey[0] === "agent-session-projection-infinite") {
    return params?.limit === PAGE_SIZE && params?.branch_mode === "head" ? "pages" : null;
  }
  if (queryKey[0] === "agent-session-event-bodies") return Array.isArray(queryKey[2]) ? "bodies" : null;
  return null;
}

function sameTranscript(left: AgentSessionProjectionResponse, right: AgentSessionProjectionResponse): boolean {
  return left.generation_id === right.generation_id && left.head_session_id === right.head_session_id;
}

export type TranscriptPersistence = {
  /** Startup: seed the most recently used sessions so the rail and ⌘K paint from disk. */
  paintRecentSessions: () => Promise<void>;
  stop: () => void;
};

export function startTranscriptPersistence(
  queryClient: QueryClient,
  { writeDelayMs = WRITE_DELAY_MS }: { writeDelayMs?: number } = {},
): TranscriptPersistence {
  /** Data objects this module put in the cache: never written back, never trusted as fresh. */
  const seeded = new WeakSet<object>();
  const olderPagesTried = new Set<string>();
  const pendingWrites = new Map<string, number>();

  const scheduleWrite = (key: string, write: () => Promise<void>) => {
    const existing = pendingWrites.get(key);
    if (existing !== undefined) window.clearTimeout(existing);
    pendingWrites.set(
      key,
      window.setTimeout(() => {
        pendingWrites.delete(key);
        void write();
      }, writeDelayMs),
    );
  };

  const writeWorkspace = (sessionId: string) =>
    scheduleWrite(`workspace:${sessionId}`, async () => {
      const workspace = queryClient.getQueryData<AgentSessionWorkspaceResponse>(workspaceKey(sessionId));
      if (!workspace || seeded.has(workspace)) return;
      await writeCachedTranscript(sessionId, "workspace", { workspace, savedAt: Date.now() } satisfies WorkspaceRecord);
    });

  const writePages = (sessionId: string) =>
    scheduleWrite(`pages:${sessionId}`, async () => {
      const data = queryClient.getQueryData<ProjectionPages>(pagesKey(sessionId));
      if (!data || seeded.has(data) || data.pages.length < 2) return;
      await writeCachedTranscript(sessionId, "pages", {
        pages: data.pages.slice(-PAGES_PER_SESSION),
        pageParams: data.pageParams.slice(-PAGES_PER_SESSION),
        savedAt: Date.now(),
      } satisfies PagesRecord);
    });

  const writeBodies = (sessionId: string, cursors: string[], response: SessionEventBodiesResponse) =>
    scheduleWrite(`bodies:${sessionId}:${cursors.join(",")}`, async () => {
      if (!response.events.length) return;
      const stored = (await readCachedTranscript<BodiesRecord>(sessionId, "bodies"))?.entries ?? [];
      const id = cursors.join(",");
      const entries = [...stored.filter((entry) => entry.cursors.join(",") !== id), { cursors, response }];
      await writeCachedTranscript(sessionId, "bodies", { entries: entries.slice(-BODIES_PER_SESSION) } satisfies BodiesRecord);
    });

  /** Seed a session's workspace (and its tail page) from disk, unless the network got there first. */
  const paintFromDisk = async (sessionId: string): Promise<void> => {
    if (queryClient.getQueryData(workspaceKey(sessionId)) !== undefined) return;
    const record = await readCachedTranscript<WorkspaceRecord>(sessionId, "workspace");
    if (!record?.workspace || queryClient.getQueryData(workspaceKey(sessionId)) !== undefined) return;
    seeded.add(record.workspace);
    // `updatedAt` from disk keeps the entry stale: the mounted page refetches.
    queryClient.setQueryData(workspaceKey(sessionId), record.workspace, { updatedAt: record.savedAt });
    const tail = record.workspace.projection;
    if (tail && queryClient.getQueryData(pagesKey(sessionId)) === undefined) {
      const pages: ProjectionPages = { pages: [tail], pageParams: [{ anchor: "tail" }] };
      seeded.add(pages);
      queryClient.setQueryData(pagesKey(sessionId), pages, { updatedAt: record.savedAt });
    }
  };

  /** Stitch stored older pages above a network-fresh tail, and restore opened tool bodies. */
  const restoreOlderPages = async (sessionId: string): Promise<void> => {
    olderPagesTried.add(sessionId);
    const bodies = await readCachedTranscript<BodiesRecord>(sessionId, "bodies");
    for (const entry of bodies?.entries ?? []) {
      const key = ["agent-session-event-bodies", sessionId, entry.cursors];
      if (queryClient.getQueryData(key) === undefined) queryClient.setQueryData(key, entry.response);
    }
    const record = await readCachedTranscript<PagesRecord>(sessionId, "pages");
    const current = queryClient.getQueryData<ProjectionPages>(pagesKey(sessionId));
    if (!record?.pages.length || !current || current.pages.length !== 1 || seeded.has(current)) return;
    const tail = current.pages[0];
    const storedTail = record.pages[record.pages.length - 1];
    if (!sameTranscript(storedTail, tail)) {
      // Another generation or head session: those pages describe history
      // this session no longer has.
      await forgetCachedSession(sessionId);
      return;
    }
    const merged = stitchProjectionTail({ pages: record.pages, pageParams: record.pageParams }, tail);
    if (!merged || merged.pages.length < 2) return;
    queryClient.setQueryData<ProjectionPages>(pagesKey(sessionId), (latest) =>
      latest === current ? merged : latest,
    );
  };

  const onQuery = (query: Query, eventType: "added" | "updated") => {
    const kind = persistedKind(query.queryKey);
    if (!kind) return;
    const sessionId = query.queryKey[1] as string;
    const data = query.state.data as object | undefined;

    if (kind === "workspace") {
      if (eventType === "added" && data === undefined) void paintFromDisk(sessionId);
      if (data && !seeded.has(data) && query.state.status === "success") writeWorkspace(sessionId);
      return;
    }
    if (kind === "bodies") {
      if (data && query.state.status === "success") {
        writeBodies(sessionId, query.queryKey[2] as string[], data as SessionEventBodiesResponse);
      }
      return;
    }
    // pages
    if (!data || seeded.has(data)) return;
    const pages = data as ProjectionPages;
    if (
      pages.pages.length === 1 &&
      !olderPagesTried.has(sessionId) &&
      query.state.fetchStatus === "idle" &&
      query.getObserversCount() > 0
    ) {
      void restoreOlderPages(sessionId);
    }
    writePages(sessionId);
  };

  const unsubscribe = queryClient.getQueryCache().subscribe((event) => {
    if (event.type === "added") onQuery(event.query, "added");
    else if (event.type === "updated" || event.type === "observerAdded") onQuery(event.query, "updated");
  });
  return {
    paintRecentSessions: async () => {
      // Sessions already asked for while the user was still loading, then the
      // most recently used ones.
      const waiting = queryClient
        .getQueryCache()
        .findAll({ queryKey: ["agent-session-workspace"] })
        .filter((query) => persistedKind(query.queryKey) === "workspace" && query.state.data === undefined)
        .map((query) => query.queryKey[1] as string);
      const recent = (await listCachedSessions()).slice(0, STARTUP_PAINT_COUNT);
      await Promise.all(Array.from(new Set([...waiting, ...recent])).map((sessionId) => paintFromDisk(sessionId)));
    },
    stop: () => {
      unsubscribe();
      for (const timer of pendingWrites.values()) window.clearTimeout(timer);
      pendingWrites.clear();
    },
  };
}
