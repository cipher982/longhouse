import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes } from "react-router";
import type { MachineSummary, MachinesSummaryResponse } from "@/shared/api/index";
import type * as ApiIndex from "@/shared/api/index";
import { TestRouter } from "@/shared/test/test-utils";
import MachinesPage from "../MachinesPage";
import MachineDetailPage from "../MachineDetailPage";

const api = vi.hoisted(() => ({ listMachineSummaries: vi.fn(), listMachines: vi.fn(), fetchRunners: vi.fn() }));

vi.mock("@/shared/api/index", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiIndex>()),
  listMachineSummaries: api.listMachineSummaries,
  listMachines: api.listMachines,
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

function renderPage(detail = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <TestRouter initialEntries={[detail ? "/machines/cinder" : "/machines"]}>
        <Routes>
          <Route path="/machines" element={<MachinesPage />} />
          <Route path="/machines/:deviceId" element={<MachineDetailPage />} />
        </Routes>
      </TestRouter>
    </QueryClientProvider>,
  );
  return { ...view, client };
}

const offline = { online: false, control_channel_status: "disconnected", launch: { providers: [], blocked_by: "control_down" } };

describe("MachinesPage", () => {
  beforeEach(() => {
    api.listMachineSummaries.mockReset();
    api.listMachines.mockReset().mockResolvedValue({ machines: [] });
    api.fetchRunners.mockReset().mockResolvedValue([]);
  });

  it("shows known machines while activity and sync are loading", async () => {
    let resolveSummary!: (value: MachinesSummaryResponse) => void;
    api.listMachineSummaries.mockImplementation(
      () => new Promise<MachinesSummaryResponse>((resolve) => { resolveSummary = resolve; }),
    );
    api.listMachines.mockResolvedValue({ machines: [summary("cinder").machine] });
    renderPage();

    const row = await screen.findByTestId("machine-directory-row-cinder");
    expect(row).toHaveTextContent("cinder");
    expect(screen.getByRole("status")).toHaveTextContent("Loading activity and sync");
    expect(within(row).queryByText("No recent sessions")).toBeNull();

    await act(async () => resolveSummary(response([summary("cinder")])));
    expect(await screen.findByTestId("machine-row-cinder")).toHaveTextContent("No recent sessions");
  });

  it("shows directory-backed machine details while activity loads", async () => {
    let resolveSummary!: (value: MachinesSummaryResponse) => void;
    api.listMachineSummaries.mockImplementation(
      () => new Promise<MachinesSummaryResponse>((resolve) => { resolveSummary = resolve; }),
    );
    api.listMachines.mockResolvedValue({ machines: [summary("cinder").machine] });
    renderPage(true);

    expect(await screen.findByTestId("machine-name")).toHaveTextContent("cinder");
    expect(screen.getByRole("status")).toHaveTextContent("Loading activity and sync");
    expect(screen.queryByText("Last 14 days")).toBeNull();

    await act(async () => resolveSummary(response([summary("cinder")])));
    expect(await screen.findByText("Last 14 days")).toBeInTheDocument();
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

  it("retains independently available machines and matches their runners during a summary outage", async () => {
    api.listMachineSummaries.mockRejectedValue(new Error("The session catalog is restarting."));
    api.listMachines.mockResolvedValue({ machines: [summary("cinder").machine] });
    api.fetchRunners.mockResolvedValue([
      { id: 1, name: "cinder", status: "online", availability_policy: "on_demand" },
      { id: 2, name: "clifford", status: "online", availability_policy: "always_on" },
    ]);
    renderPage();

    expect(await screen.findByRole("button", { name: "Try again" }, { timeout: 5000 })).toBeInTheDocument();
    const row = await screen.findByTestId("machine-row-cinder", {}, { timeout: 5000 });
    expect(within(row).getByRole("link")).toHaveAttribute("href", "/machines/cinder");
    const unmatched = await screen.findByTestId("machines-unmatched-runners");
    expect(unmatched).not.toHaveTextContent("cinder");
    expect(unmatched).toHaveTextContent("clifford");
    expect(screen.queryByTestId("machines-summary")).toBeNull();
    expect(screen.queryByTestId("machines-connect-first-button")).toBeNull();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("keeps known machine actions and readiness without inventing an activity summary", async () => {
    api.listMachineSummaries.mockRejectedValue(new Error("The session catalog is restarting."));
    api.listMachines.mockResolvedValue({ machines: [summary("cinder").machine] });
    renderPage(true);

    expect(await screen.findByTestId("machine-name", {}, { timeout: 5000 })).toHaveTextContent("cinder");
    expect(screen.getByRole("link", { name: "Open sessions" })).toHaveAttribute("href", "/timeline?device_id=cinder");
    expect(screen.getByTestId("machine-new-session")).toBeInTheDocument();
    expect(screen.getByText("Claude")).toBeInTheDocument();
    expect(screen.queryByText("No upload reports from this machine in the last 30 days.")).toBeNull();
    expect(screen.queryByText("Last 14 days")).toBeNull();
    expect(await screen.findByRole("button", { name: "Retry" }, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("unavailable");
  });

  it("marks a retained empty directory as last known after refresh fails", async () => {
    api.listMachineSummaries.mockResolvedValue(response([]));
    const { client } = renderPage();
    await screen.findByTestId("machines-connect-first-button");
    api.listMachineSummaries.mockRejectedValue(new Error("Refresh unavailable"));
    await act(async () => { await client.invalidateQueries({ queryKey: ["machine-summaries"] }); });

    expect(await screen.findByRole("status")).toHaveTextContent("Showing what Longhouse knew");
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("marks retained detail facts as last known and keeps the scoped repair instruction", async () => {
    api.listMachineSummaries.mockResolvedValue(response([summary("cinder", {
      sync: { status: "broken", stale: false, history: { state: "unavailable" } } as MachineSummary["sync"],
    })]));
    const { client } = renderPage(true);
    await screen.findByTestId("machine-name");
    expect(screen.getByText(/longhouse local-health/)).toBeInTheDocument();
    api.listMachineSummaries.mockRejectedValue(new Error("Refresh unavailable"));
    await act(async () => { await client.invalidateQueries({ queryKey: ["machine-summaries"] }); });

    expect(await screen.findByRole("status")).toHaveTextContent("Showing what Longhouse knew");
    expect(screen.getByRole("link", { name: "Open sessions" })).toBeInTheDocument();
  });
});
