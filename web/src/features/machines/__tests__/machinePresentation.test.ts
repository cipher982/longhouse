import { describe, expect, it } from "vitest";
import type { MachineDirectoryEntry, MachineSync } from "@/shared/api/index";
import { historyLine, isImporting, machineAgents, runnerForMachine, unmatchedRunners } from "../machinePresentation";

function machine(overrides: Partial<MachineDirectoryEntry> = {}): MachineDirectoryEntry {
  return {
    device_id: "cinder",
    machine_name: "cinder",
    online: true,
    control_channel_status: "connected",
    launch: { providers: [{ provider: "claude" }], default_provider: "claude", blocked_by: null, unavailable_providers: [] },
    ...overrides,
  } as MachineDirectoryEntry;
}

const offline = { online: false, control_channel_status: "disconnected" as const, launch: { providers: [], blocked_by: "control_down" as const } };

describe("machineAgents", () => {
  it("orders launchable agents by use and appends signed-out ones dimmed", () => {
    const agents = machineAgents(
      machine({
        launch: {
          providers: [{ provider: "claude" }, { provider: "omp" }],
          blocked_by: null,
          unavailable_providers: [{ provider: "codex", reason: "not_authenticated", remediation: null }],
        },
      }),
      { daily: [{ date: "2026-10-03", total: 5, by_provider: { omp: 4, claude: 1 } }] },
    );
    expect(agents).toEqual([
      { provider: "omp", unavailable: false },
      { provider: "claude", unavailable: false },
      { provider: "codex", unavailable: true },
    ]);
  });

  it("shows what an offline machine ran, since it reports no agents", () => {
    const agents = machineAgents(machine(offline), {
      daily: [{ date: "2026-10-01", total: 2, by_provider: { opencode: 2 } }],
    });
    expect(agents).toEqual([{ provider: "opencode", unavailable: false }]);
  });

  it("never lends a connected machine the agents it used to run", () => {
    const agents = machineAgents(
      machine({ launch: { providers: [], blocked_by: "providers_not_ready", unavailable_providers: [] } }),
      { daily: [{ date: "2026-10-01", total: 2, by_provider: { opencode: 2 } }] },
    );
    expect(agents).toEqual([]);
  });
});

describe("runners", () => {
  const runners = [
    { id: 1, name: "cinder", status: "online", availability_policy: "on_demand" },
    { id: 2, name: "clifford", status: "online", availability_policy: "always_on" },
    { id: 3, name: "lh-vm-canary-1", status: "offline", availability_policy: "ephemeral" },
    { id: 4, name: "lh-vm-canary-2", status: "revoked", availability_policy: "ephemeral" },
    { id: 5, name: "cube", status: "revoked", availability_policy: "on_demand" },
  ];

  it("ties a runner to the machine with its name and never to a revoked one", () => {
    expect(runnerForMachine(runners, machine())?.id).toBe(1);
    expect(runnerForMachine(runners, machine({ device_id: "cube", machine_name: "cube" }))).toBeNull();
  });

  it("lists only live or long-lived runners that match no machine", () => {
    expect(unmatchedRunners(runners, [machine()]).map((runner) => runner.name)).toEqual(["clifford"]);
  });
});

describe("eligible-history completion", () => {
  function sync(state: string, remaining: number | null = null): MachineSync {
    return {
      stale: false,
      history: { state, source_count: null, remaining_bytes: remaining, remaining_records: remaining },
    } as MachineSync;
  }

  it.each(["discovering", "inventory_ready", "importing", "backpressured"])(
    "does not infer completion from missing or zero progress in %s",
    (state) => {
      for (const remaining of [null, 0]) {
        expect(historyLine(sync(state, remaining)).tone).not.toBe("live");
        expect(isImporting(sync(state, remaining))).toBe(true);
      }
    },
  );

  it("uses current as completion authority and does not treat stale progress as active import", () => {
    expect(historyLine(sync("current")).tone).toBe("live");
    expect(isImporting(sync("current"))).toBe(false);
    expect(isImporting({ ...sync("importing", 1000), stale: true })).toBe(false);
  });
});
