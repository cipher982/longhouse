import { describe, expect, it } from "bun:test";

import { ompProviderIsIdle } from "./longhouse-omp-helm";

describe("ompProviderIsIdle", () => {
  it("uses the live context before any agent_end evidence exists", () => {
    expect(ompProviderIsIdle(undefined, false)).toBe(false);
    expect(ompProviderIsIdle(undefined, true)).toBe(true);
  });

  it("keeps an explicit continuation active through a transient idle context", () => {
    expect(ompProviderIsIdle(false, false)).toBe(false);
    expect(ompProviderIsIdle(false, true)).toBe(false);
  });

  it("keeps an explicit terminal result idle even if context is stale", () => {
    expect(ompProviderIsIdle(true, false)).toBe(true);
    expect(ompProviderIsIdle(true, true)).toBe(true);
  });
});
