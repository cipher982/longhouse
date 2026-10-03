import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MachineSummary, MachinesSummaryResponse } from "@/shared/api/index";
import type * as ApiIndex from "@/shared/api/index";
import { TestRouter } from "@/shared/test/test-utils";
import MachinesPage from "../MachinesPage";

const api = vi.hoisted(() => ({ listMachineSummaries: vi.fn(), fetchRunners: vi.fn() }));

vi.mock("@/shared/api/index", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiIndex>()),
  listMachineSummaries: api.listMachineSummaries,
  fetchRunners: api.fetchRunners,
}));

const DAYS = Array.from({ length: 14 }, (_, index) => ({ date: `2026-09-${String(20 + index).padStart(2, "0")}`, total: 0, by_provider: {} }));

function summary(deviceId: string, overrides: { machine?: object; activity?: object; sync?: MachineSummary["sync"] } = {}): MachineSummary {
  return {
    machine: {
      device_id: deviceId,
      machine_name: deviceId,
      online: true,
      control_channel_status: "connected",
      launch: { providers: [{ provider: "claude" }], default_provider: "claude", blocked_by: null, unavailable_providers: [] },
      ...overrides.machine,
    },
    activity: {
      sessions_started: 0,
      daily: DAYS,
      top_projects: [],
      latest_session: null,
      live_count: 0,
      live_sessions: [],
      ...overrides.activity,
    },
    sync: overrides.sync ?? null,
  } as MachineSummary;
}

function response(machines: MachineSummary[]): MachinesSummaryResponse {
  return { generated_at: "2026-10-03T16:00:00Z", days: 14, utc_offset_minutes: 0, first_day: "2026-09-20", last_day: "2026-10-03", machines };
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TestRouter>
        <MachinesPage />
      </TestRouter>
    </QueryClientProvider>,
  );
}

const offline = { online: false, control_channel_status: "disconnected", launch: { providers: [], blocked_by: "control_down" } };

describe("MachinesPage", () => {
  beforeEach(() => {
    api.listMachineSummaries.mockReset();
    api.fetchRunners.mockReset().mockResolvedValue([]);
  });

  it("lists active machines with their live work and folds quiet ones into one line", async () => {
    api.listMachineSummaries.mockResolvedValue(
      response([
        summary("cinder", {
          activity: {
            sessions_started: 254,
            live_count: 9,
            live_sessions: [
              { session_id: "a", title: "Broken signup page deep dive", project: "zerg", provider: "omp", last_activity_at: null, activity_state: "thinking" },
            ],
          },
        }),
        summary("clifford-sauron", { machine: offline, activity: { sessions_started: 8 } }),
        summary("cube-canary", { machine: offline }),
      ]),
    );
    renderPage();

    const list = await screen.findByTestId("machine-list");
    expect(within(list).getByTestId("machine-row-cinder")).toHaveTextContent("9 live");
    expect(within(list).getByRole("link", { name: /Broken signup page deep dive/ })).toHaveAttribute("href", "/timeline/a");
    expect(within(list).getByTestId("machine-row-clifford-sauron")).toHaveTextContent("Offline");
    expect(within(list).queryByTestId("machine-row-cube-canary")).toBeNull();
    expect(screen.getByTestId("machines-quiet")).toHaveTextContent("Not seen recently: cube-canary");
    expect(screen.getByTestId("machines-summary")).toHaveTextContent("9 live on 1 machine · 262 sessions in the last 14 days");

    await userEvent.click(screen.getByRole("button", { name: "Show all" }));
    expect(screen.getByTestId("machine-row-cube-canary")).toBeInTheDocument();
  });

  it("puts a signed-out agent's fix on the machine's row", async () => {
    api.listMachineSummaries.mockResolvedValue(
      response([
        summary("cube-bench", {
          machine: {
            launch: {
              providers: [{ provider: "claude" }],
              blocked_by: null,
              unavailable_providers: [{ provider: "codex", reason: "not_authenticated", remediation: null }],
            },
          },
        }),
      ]),
    );
    renderPage();

    const row = await screen.findByTestId("machine-row-cube-bench");
    expect(row).toHaveTextContent("Codex signed out");
    expect(row).toHaveTextContent("Sign in to Codex on cube-bench");
    expect(within(row).getByRole("link", { name: "Fix" })).toHaveAttribute("href", "/machines/cube-bench");
  });

  it("names runners that belong to no machine, and hides revoked and departed ephemeral ones", async () => {
    api.listMachineSummaries.mockResolvedValue(response([summary("cinder")]));
    api.fetchRunners.mockResolvedValue([
      { id: 1, name: "cinder", status: "online", availability_policy: "on_demand" },
      { id: 2, name: "clifford", status: "online", availability_policy: "always_on" },
      { id: 3, name: "lh-vm-canary", status: "offline", availability_policy: "ephemeral" },
      { id: 4, name: "old", status: "revoked", availability_policy: "always_on" },
    ]);
    renderPage();

    expect(await screen.findByTestId("machines-unmatched-runners")).toHaveTextContent("Runners not tied to a machine: clifford (online)");
  });

  it("offers to connect a first machine on an empty host", async () => {
    api.listMachineSummaries.mockResolvedValue(response([]));
    renderPage();

    expect(await screen.findByText("Connect your first machine")).toBeInTheDocument();
    expect(screen.getByTestId("machines-connect-first-button")).toBeInTheDocument();
  });

  it("says the read failed instead of showing an empty host", async () => {
    api.listMachineSummaries.mockRejectedValue(new Error("The session catalog is restarting."));
    renderPage();

    // The page retries once before it gives up.
    expect(await screen.findByText("Machines are unavailable right now", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByText("The session catalog is restarting.")).toBeInTheDocument();
    expect(screen.queryByText("Connect your first machine")).toBeNull();
  });
});
