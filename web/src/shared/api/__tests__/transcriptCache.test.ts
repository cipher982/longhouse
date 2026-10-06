import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  MAX_CACHED_SESSIONS,
  listCachedSessions,
  readCachedTranscript,
  setTranscriptCacheBackendForTests,
  setTranscriptCacheUser,
  wipeTranscriptCache,
  writeCachedTranscript,
  type TranscriptCacheBackend,
} from "../transcriptCache";

function memoryBackend(): TranscriptCacheBackend & { store: Map<string, unknown> } {
  const store = new Map<string, unknown>();
  return {
    store,
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

describe("transcriptCache", () => {
  let backend: ReturnType<typeof memoryBackend>;

  beforeEach(async () => {
    backend = memoryBackend();
    setTranscriptCacheBackendForTests(backend);
    await setTranscriptCacheUser(1, "https://a.example");
  });

  afterEach(async () => {
    await setTranscriptCacheUser(null);
  });

  it("reads back what it wrote, for the signed-in user only", async () => {
    await writeCachedTranscript("s1", "workspace", { hello: "one" });
    expect(await readCachedTranscript("s1", "workspace")).toEqual({ hello: "one" });

    await setTranscriptCacheUser(2, "https://a.example");
    expect(await readCachedTranscript("s1", "workspace")).toBeNull();
    // Another account signing in removes the first account's records outright.
    expect(Array.from(backend.store.keys()).some((key) => key.includes("https://a.example|1"))).toBe(false);
  });

  it("does nothing while signed out", async () => {
    await setTranscriptCacheUser(null);
    await writeCachedTranscript("s1", "workspace", { hello: "one" });
    expect(backend.store.size).toBe(0);
    expect(await readCachedTranscript("s1", "workspace")).toBeNull();
  });

  it("sign-out wipes every record", async () => {
    await writeCachedTranscript("s1", "workspace", { a: 1 });
    await writeCachedTranscript("s1", "pages", { b: 2 });
    await wipeTranscriptCache();
    expect(backend.store.size).toBe(0);
    await setTranscriptCacheUser(1, "https://a.example");
    expect(await readCachedTranscript("s1", "workspace")).toBeNull();
  });

  it("evicts the least recently used sessions past the count cap", async () => {
    let now = 1_000;
    const realNow = Date.now;
    Date.now = () => (now += 1);
    try {
      for (let i = 0; i < MAX_CACHED_SESSIONS + 3; i += 1) {
        await writeCachedTranscript(`s${i}`, "workspace", { i });
        if (i === MAX_CACHED_SESSIONS - 1) await readCachedTranscript("s0", "workspace"); // s0 used again: kept
      }
    } finally {
      Date.now = realNow;
    }
    const kept = await listCachedSessions();
    expect(kept).toHaveLength(MAX_CACHED_SESSIONS);
    expect(kept).toContain("s0");
    expect(kept).not.toContain("s1");
    expect(kept).not.toContain("s2");
    expect(kept).not.toContain("s3");
    expect(await readCachedTranscript("s1", "workspace")).toBeNull();
  });

  it("evicts by bytes, but never the session just written", async () => {
    const big = "x".repeat(25 * 1024 * 1024);
    await writeCachedTranscript("old", "workspace", { big });
    await writeCachedTranscript("new", "workspace", { big });
    expect(await listCachedSessions()).toEqual(["new"]);
  });

  it("degrades to no cache when IndexedDB is unavailable", async () => {
    setTranscriptCacheBackendForTests(null);
    await expect(writeCachedTranscript("s1", "workspace", { a: 1 })).resolves.toBeUndefined();
    expect(await readCachedTranscript("s1", "workspace")).toBeNull();
    expect(await listCachedSessions()).toEqual([]);
  });

  it("degrades to no cache when IndexedDB throws", async () => {
    const failing: TranscriptCacheBackend = {
      get: async () => {
        throw new Error("quota");
      },
      put: async () => {
        throw new Error("quota");
      },
      delete: async () => {
        throw new Error("quota");
      },
      keys: async () => {
        throw new Error("quota");
      },
    };
    setTranscriptCacheBackendForTests(failing);
    await expect(writeCachedTranscript("s1", "workspace", { a: 1 })).resolves.toBeUndefined();
    expect(await readCachedTranscript("s1", "workspace")).toBeNull();
  });
});
