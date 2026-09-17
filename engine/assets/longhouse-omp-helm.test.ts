import { describe, expect, it } from "bun:test";

const identityKeys = [
  "LONGHOUSE_OMP_HELM_CHANNEL_PATH",
  "LONGHOUSE_OMP_HELM_CHANNEL_TOKEN",
  "LONGHOUSE_MANAGED_SESSION_ID",
] as const;
const previousIdentity = Object.fromEntries(identityKeys.map((key) => [key, process.env[key]]));
Object.assign(process.env, {
  LONGHOUSE_OMP_HELM_CHANNEL_PATH: "/tmp/omp-helm-test.sock",
  LONGHOUSE_OMP_HELM_CHANNEL_TOKEN: "omp-helm-test-token",
  LONGHOUSE_MANAGED_SESSION_ID: "omp-helm-test-session",
});
// Dynamic import is intentional: the extension validates launch-scoped identity at module load.
const { agentEndIsTerminal, ompProviderIsIdle } = await import("./longhouse-omp-helm");
for (const key of identityKeys) {
  const value = previousIdentity[key];
  if (value === undefined) delete process.env[key];
  else process.env[key] = value;
}

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

describe("agentEndIsTerminal", () => {
  it("uses the same conservative terminal decision for malformed lifecycle fields", () => {
    expect(agentEndIsTerminal({ type: "agent_end" })).toBe(true);
    expect(agentEndIsTerminal({ type: "agent_end", isTerminal: true })).toBe(true);
    expect(agentEndIsTerminal({ type: "agent_end", willContinue: true })).toBe(false);
    expect(agentEndIsTerminal({ type: "agent_end", willContinue: null })).toBe(false);
    expect(agentEndIsTerminal({ type: "agent_end", isTerminal: undefined })).toBe(false);
    expect(agentEndIsTerminal({ type: "agent_end", isTerminal: "true" })).toBe(false);
  });
});
