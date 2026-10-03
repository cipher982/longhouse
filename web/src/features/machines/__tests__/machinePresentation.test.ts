import { describe, expect, it } from "vitest";
import type { MachineDirectoryEntry } from "@/shared/api/index";
import { machineAgents, machineStatus, runnerForMachine, unmatchedRunners } from "../machinePresentation";

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

describe("machineStatus", () => {
  it("reads live sessions first, and still carries a sign-in hint", () => {
    const status = machineStatus({
      machine: machine({
        launch: {
          providers: [{ provider: "claude" }],
          blocked_by: null,
          unavailable_providers: [{ provider: "codex", reason: "not_authenticated", remediation: null }],
        },
      }),
      activity: { live_count: 9, sessions_started: 254 },
    });
    expect(status).toMatchObject({ tone: "live", label: "9 live", hint: "Sign in to Codex on cinder" });
  });

  it("asks for sign-in when a provider is signed out", () => {
    const status = machineStatus({
      machine: machine({
        launch: {
          providers: [{ provider: "claude" }],
          blocked_by: null,
          unavailable_providers: [{ provider: "codex", reason: "not_authenticated", remediation: "Run codex login" }],
        },
      }),
      activity: { live_count: 0, sessions_started: 0 },
    });
    expect(status).toMatchObject({ tone: "attention", label: "Codex signed out", hint: "Run codex login" });
  });

  it("does not nag about a CLI the machine never had while other agents can run", () => {
    const status = machineStatus({
      machine: machine({
        launch: {
          providers: [{ provider: "claude" }],
          blocked_by: null,
          unavailable_providers: [{ provider: "antigravity", reason: "cli_missing", remediation: null }],
        },
      }),
    });
    expect(status).toMatchObject({ tone: "idle", label: "Online, idle", hint: null });
  });

  it("names a missing CLI when nothing else can start a session", () => {
    const status = machineStatus({
      machine: machine({
        launch: {
          providers: [],
          blocked_by: "providers_not_ready",
          unavailable_providers: [{ provider: "claude", reason: "cli_missing", remediation: null }],
        },
      }),
    });
    expect(status).toMatchObject({ tone: "attention", label: "Claude not installed" });
  });

  it("reserves red for faults that need repair", () => {
    expect(machineStatus({ machine: machine({ launch: { providers: [], blocked_by: "auth_failed" } }) }).tone).toBe("fault");
    expect(machineStatus({ machine: machine(), sync: { status: "broken", stale: false } }).tone).toBe("fault");
    // An old broken report on a machine that has since gone quiet is not a current fault.
    expect(machineStatus({ machine: machine(offline), sync: { status: "broken", stale: true } }).tone).toBe("off");
  });

  it("keeps an ordinary offline machine gray and folds it away only when nothing happened", () => {
    expect(machineStatus({ machine: machine(offline), activity: { live_count: 0, sessions_started: 0 } })).toMatchObject({
      tone: "off",
      label: "Offline",
      quiet: true,
    });
    expect(machineStatus({ machine: machine(offline), activity: { live_count: 0, sessions_started: 8 } }).quiet).toBe(false);
  });

  it("calls a disconnected machine that still ships 'Sync only'", () => {
    expect(machineStatus({ machine: machine(offline), sync: { status: "healthy", stale: false } })).toMatchObject({
      tone: "quiet",
      label: "Sync only",
      quiet: false,
    });
  });
});

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
