import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InfiniteQueryObserver, QueryClient, QueryObserver, type InfiniteData } from "@tanstack/react-query";

import type { AgentSessionProjectionResponse, AgentSessionWorkspaceResponse } from "@/shared/api/agents";
import {
  readCachedTranscript,
  setTranscriptCacheBackendForTests,
  setTranscriptCacheUser,
  writeCachedTranscript,
  type TranscriptCacheBackend,
} from "@/shared/api/transcriptCache";
import { agentSessionWorkspaceQueryOptions } from "@/shared/api/useAgentSessions";
import { startTranscriptPersistence, type TranscriptPersistence } from "../transcriptPersistence";

function memoryBackend(): TranscriptCacheBackend {
  const store = new Map<string, unknown>();
  return {
    get: async (key) => store.get(key),
    put: async (key, value) => {
      store.set(key, value);
    },
    delete: async (key) => {
      store.delete(key);
    },
    keys: async () => Array.from(store.keys()),
  };
}

const SESSION = "s1";
const workspaceKey = agentSessionWorkspaceQueryOptions(SESSION, { limit: 200 }).queryKey;
const pagesKey = ["agent-session-projection-infinite", SESSION, { limit: 200, branch_mode: "head" }] as const;

type Page = AgentSessionProjectionResponse;

function item(n: number) {
  return {
    kind: "event",
    session_id: SESSION,
    timestamp: `2026-10-06T00:00:${String(n).padStart(2, "0")}Z`,
    event: { id: `e${n}`, cursor: `c${n}`, role: "assistant", content_text: `event ${n}` },
  };
}

function page(from: number, to: number, generation = "g1"): Page {
  const items = [];
  for (let n = from; n <= to; n += 1) items.push(item(n));
  return {
    items,
    generation_id: generation,
    head_session_id: SESSION,
    focus_session_id: SESSION,
    has_more: from > 1,
    next_cursor: from > 1 ? `c${from}` : null,
    total: 100,
    abandoned_events: 0,
  } as unknown as Page;
}

function workspace(tail: Page, label: string): AgentSessionWorkspaceResponse {
  return { session: { id: SESSION, label }, thread: { sessions: [] }, projection: tail } as unknown as AgentSessionWorkspaceResponse;
}

function pagesObserver(client: QueryClient, queryFn: () => Promise<Page>) {
  return new InfiniteQueryObserver<Page, Error, InfiniteData<Page>, readonly unknown[], { anchor: "tail"; cursor?: string }>(
    client,
    {
      queryKey: pagesKey,
      queryFn,
      initialPageParam: { anchor: "tail" },
      getPreviousPageParam: (first) => (first.has_more && first.next_cursor ? { anchor: "tail", cursor: first.next_cursor } : undefined),
      getNextPageParam: () => undefined,
      staleTime: 10_000,
    },
  );
}

describe("transcript persistence", () => {
  let client: QueryClient;
  let persistence: TranscriptPersistence;
  const cleanups: (() => void)[] = [];

  beforeEach(async () => {
    setTranscriptCacheBackendForTests(memoryBackend());
    await setTranscriptCacheUser(1, "https://a.example");
    client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    persistence = startTranscriptPersistence(client, { writeDelayMs: 5 });
  });

  afterEach(async () => {
    cleanups.splice(0).forEach((cleanup) => cleanup());
    persistence.stop();
    client.clear();
    await setTranscriptCacheUser(null);
  });

  it("a reload paints the session from disk, then the network replaces it", async () => {
    const disk = workspace(page(81, 100), "from disk");
    await writeCachedTranscript(SESSION, "workspace", { workspace: disk, savedAt: 1 });

    let answer!: (value: AgentSessionWorkspaceResponse) => void;
    const queryFn = vi.fn(() => new Promise<AgentSessionWorkspaceResponse>((resolve) => (answer = resolve)));
    const observer = new QueryObserver(client, { queryKey: workspaceKey, queryFn, staleTime: 10_000 });
    cleanups.push(observer.subscribe(() => undefined));

    // The disk copy is on screen while the network request is still open.
    await vi.waitFor(() => expect(client.getQueryData(workspaceKey)).toBe(disk));
    expect(queryFn).toHaveBeenCalledTimes(1);
    // Seeded stale, so the transcript query the page mounts next refetches too.
    expect(client.getQueryState(pagesKey)?.dataUpdatedAt).toBe(1);
    expect(client.getQueryCache().find({ queryKey: pagesKey })?.isStaleByTime(10_000)).toBe(true);

    const network = workspace(page(82, 101), "from network");
    answer(network);
    // Structural sharing keeps unchanged subtrees, so compare by value.
    await vi.waitFor(() => expect(client.getQueryData(workspaceKey)).toEqual(network));
    // And the network copy is what gets written back.
    await vi.waitFor(async () =>
      expect((await readCachedTranscript<{ workspace: AgentSessionWorkspaceResponse }>(SESSION, "workspace"))?.workspace.session).toEqual(
        network.session,
      ),
    );
  });

  it("a fresh network answer is never replaced by the disk copy", async () => {
    await writeCachedTranscript(SESSION, "workspace", { workspace: workspace(page(81, 100), "disk"), savedAt: 1 });
    const network = workspace(page(82, 101), "network");
    client.setQueryData(workspaceKey, network);
    await persistence.paintRecentSessions();
    expect(client.getQueryData(workspaceKey)).toBe(network);
  });

  it("scrolling up reads stored pages before the network", async () => {
    // Stored: two older pages and the tail as they were when the session was last read.
    await writeCachedTranscript(SESSION, "pages", {
      pages: [page(41, 60), page(61, 80), page(81, 100)],
      pageParams: [{ anchor: "tail", cursor: "c61" }, { anchor: "tail", cursor: "c81" }, { anchor: "tail" }],
      savedAt: 1,
    });
    // The network tail has moved on by one event.
    const queryFn = vi.fn(async () => page(82, 101));
    const observer = pagesObserver(client, queryFn);
    cleanups.push(observer.subscribe(() => undefined));

    await vi.waitFor(() => expect(client.getQueryData<InfiniteData<Page>>(pagesKey)?.pages.length).toBe(4));
    const data = client.getQueryData<InfiniteData<Page>>(pagesKey)!;
    const ids = data.pages.flatMap((p) => p.items.map((it) => it.event!.id));
    expect(ids[0]).toBe("e41");
    expect(ids.at(-1)).toBe("e101");
    expect(new Set(ids).size).toBe(ids.length);
    // Only the tail came from the network.
    expect(queryFn).toHaveBeenCalledTimes(1);
    expect(observer.getCurrentResult().hasPreviousPage).toBe(true);
  });

  it("stored pages from another generation are dropped, not shown", async () => {
    await writeCachedTranscript(SESSION, "pages", {
      pages: [page(61, 80, "old"), page(81, 100, "old")],
      pageParams: [{ anchor: "tail", cursor: "c81" }, { anchor: "tail" }],
      savedAt: 1,
    });
    const observer = pagesObserver(client, async () => page(81, 100, "new"));
    cleanups.push(observer.subscribe(() => undefined));

    await vi.waitFor(async () => expect(await readCachedTranscript(SESSION, "pages")).toBeNull());
    expect(client.getQueryData<InfiniteData<Page>>(pagesKey)?.pages).toHaveLength(1);
  });

  it("restores the tool bodies the user had opened", async () => {
    const response = { events: [{ cursor: "c5", tool_output_text: "full output" }], missing: [] };
    await writeCachedTranscript(SESSION, "bodies", { entries: [{ cursors: ["c5"], response }] });
    const observer = pagesObserver(client, async () => page(81, 100));
    cleanups.push(observer.subscribe(() => undefined));

    await vi.waitFor(() => expect(client.getQueryData(["agent-session-event-bodies", SESSION, ["c5"]])).toEqual(response));
  });

  it("writes paged history once the reader has scrolled up", async () => {
    client.setQueryData<InfiniteData<Page>>(pagesKey, {
      pages: [page(61, 80), page(81, 100)],
      pageParams: [{ anchor: "tail", cursor: "c81" }, { anchor: "tail" }],
    });
    await vi.waitFor(async () =>
      expect((await readCachedTranscript<{ pages: Page[] }>(SESSION, "pages"))?.pages).toHaveLength(2),
    );
  });

  it("works from the network alone when IndexedDB is unavailable", async () => {
    setTranscriptCacheBackendForTests(null);
    const network = workspace(page(82, 101), "network");
    const observer = new QueryObserver(client, { queryKey: workspaceKey, queryFn: async () => network });
    cleanups.push(observer.subscribe(() => undefined));
    await vi.waitFor(() => expect(client.getQueryData(workspaceKey)).toBe(network));
    await expect(persistence.paintRecentSessions()).resolves.toBeUndefined();
  });
});
