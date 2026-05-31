import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import ObservabilityPage from "../ObservabilityPage";
import config from "../../lib/config";

const mockFetch = vi.fn();
global.fetch = mockFetch;

function buildOverview(hoursBack: number) {
  return {
    generated_at: "2026-04-23T21:00:00Z",
    hours_back: hoursBack,
    slow_threshold_ms: 30000,
    stale_after_seconds: 900,
    summary: {
      completed_turns: hoursBack === 6 ? 8 : 12,
      slow_turns: hoursBack === 6 ? 1 : 2,
      durable_turns: hoursBack === 6 ? 7 : 10,
      terminal_only_turns: hoursBack === 6 ? 1 : 2,
      submit_to_send_ms: { p50: 600, p95: hoursBack === 6 ? 900 : 1200, max: 2400 },
      submit_to_active_ms: { p50: 1400, p95: 4800, max: 8200 },
      submit_to_terminal_ms: { p50: 12000, p95: hoursBack === 6 ? 26000 : 42000, max: 72000 },
      active_to_terminal_ms: { p50: 10000, p95: 36000, max: 68000 },
      terminal_to_durable_ms: { p50: 700, p95: 1800, max: 4000 },
      total_turn_time_ms: { p50: 12500, p95: hoursBack === 6 ? 28000 : 45000, max: 72000 },
    },
    providers: [
      {
        provider: "claude",
        completed_turns: 8,
        slow_turns: 2,
        durable_turns: 6,
        terminal_only_turns: 2,
        submit_to_send_ms: { p50: 700, p95: 1300, max: 2500 },
        submit_to_active_ms: { p50: 1600, p95: 5200, max: 9000 },
        submit_to_terminal_ms: { p50: 14000, p95: 48000, max: 72000 },
        active_to_terminal_ms: { p50: 11000, p95: 39000, max: 65000 },
        terminal_to_durable_ms: { p50: 800, p95: 2000, max: 3500 },
        total_turn_time_ms: { p50: 15000, p95: 49000, max: 72000 },
      },
      {
        provider: "codex",
        completed_turns: 4,
        slow_turns: 0,
        durable_turns: 4,
        terminal_only_turns: 0,
        submit_to_send_ms: { p50: 500, p95: 900, max: 1200 },
        submit_to_active_ms: { p50: 1200, p95: 2800, max: 3400 },
        submit_to_terminal_ms: { p50: 9000, p95: 18000, max: 22000 },
        active_to_terminal_ms: { p50: 7800, p95: 15000, max: 19000 },
        terminal_to_durable_ms: { p50: 600, p95: 1400, max: 1700 },
        total_turn_time_ms: { p50: 9500, p95: 19000, max: 22000 },
      },
    ],
    machines: [
      {
        device_id: "broken-machine",
        version: "0.6.0",
        last_heartbeat_at: "2026-04-23T20:58:00Z",
        heartbeat_age_seconds: 120,
        stale_after_seconds: 900,
        is_stale: false,
        status: "broken",
        status_reason: "spool_dead",
        status_summary: "1 dead-letter range(s) need repair.",
        reasons: ["spool_dead", "consecutive_failures"],
        last_ship_at: null,
        last_ship_attempt_at: "2026-04-23T20:58:00Z",
        last_ship_result: "connect_error",
        last_ship_latency_ms: 220,
        last_ship_http_status: null,
        ship_attempts_1h: 4,
        ship_successes_1h: 2,
        ship_success_rate_1h: 0.5,
        ship_rate_limited_1h: 0,
        ship_server_errors_1h: 0,
        ship_payload_rejections_1h: 0,
        ship_payload_too_large_1h: 0,
        ship_retryable_client_errors_1h: 0,
        ship_connect_errors_1h: 1,
        ship_latency_p50_ms_1h: 120,
        ship_latency_p95_ms_1h: 220,
        spool_pending: 2,
        spool_dead: 1,
        parse_errors_1h: 0,
        consecutive_failures: 1,
        disk_free_bytes: 1000,
        is_offline: false,
      },
      {
        device_id: "healthy-machine",
        version: "0.6.0",
        last_heartbeat_at: "2026-04-23T20:59:00Z",
        heartbeat_age_seconds: 60,
        stale_after_seconds: 900,
        is_stale: false,
        status: "healthy",
        status_reason: "healthy",
        status_summary: "Shipping healthy.",
        reasons: [],
        last_ship_at: "2026-04-23T20:58:50Z",
        last_ship_attempt_at: "2026-04-23T20:58:50Z",
        last_ship_result: "success",
        last_ship_latency_ms: 90,
        last_ship_http_status: 200,
        ship_attempts_1h: 5,
        ship_successes_1h: 5,
        ship_success_rate_1h: 1,
        ship_rate_limited_1h: 0,
        ship_server_errors_1h: 0,
        ship_payload_rejections_1h: 0,
        ship_payload_too_large_1h: 0,
        ship_retryable_client_errors_1h: 0,
        ship_connect_errors_1h: 0,
        ship_latency_p50_ms_1h: 70,
        ship_latency_p95_ms_1h: 100,
        spool_pending: 0,
        spool_dead: 0,
        parse_errors_1h: 0,
        consecutive_failures: 0,
        disk_free_bytes: 1000,
        is_offline: false,
      },
    ],
    machine_counts: {
      total: 2,
      healthy: 1,
      degraded: 0,
      offline: 0,
      broken: 1,
    },
    slow_turns: [
      {
        turn_id: 1,
        session_id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        request_id: "req-slowest",
        provider: "claude",
        project: "zerg",
        device_id: "broken-machine",
        device_name: "demo-machine",
        state: "durable",
        terminal_phase: "completed",
        error_code: null,
        user_submitted_at: "2026-04-23T19:00:00Z",
        completed_at: "2026-04-23T19:01:12Z",
        total_turn_time_ms: 72000,
        timing: {
          submit_to_send_ms: 1000,
          submit_to_active_ms: 5000,
          submit_to_terminal_ms: 70000,
          active_to_terminal_ms: 65000,
          terminal_to_durable_ms: 2000,
          total_turn_time_ms: 72000,
        },
        machine: {
          device_id: "broken-machine",
          status: "broken",
          status_reason: "spool_dead",
          status_summary: "1 dead-letter range(s) need repair.",
          last_heartbeat_at: "2026-04-23T20:58:00Z",
          heartbeat_age_seconds: 120,
          is_stale: false,
          version: "0.6.0",
        },
      },
    ],
    slow_turn_total: hoursBack === 6 ? 1 : 2,
  };
}

function renderObservabilityPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ObservabilityPage />
      </QueryClientProvider>
    </MemoryRouter>
  );
}

function cloneOverview(hoursBack: number) {
  return JSON.parse(JSON.stringify(buildOverview(hoursBack)));
}

function buildProductChecks() {
  return {
    checks: [
      {
        check: "machine_connected",
        verdict: "ok",
        coverage: "full",
        window: "15m",
        generated_at: "2026-04-23T21:00:00Z",
        headline: "1 recent machine connected and healthy.",
      },
      {
        check: "render_freshness",
        verdict: "ok",
        coverage: "full",
        window: "15m",
        generated_at: "2026-04-23T21:00:00Z",
        headline: "Render beacons are fresh; latest arrived 10s ago.",
      },
      {
        check: "live_preview",
        verdict: "ok",
        coverage: "full",
        window: "15m",
        generated_at: "2026-04-23T21:00:00Z",
        headline: "Live preview latency is within threshold.",
      },
    ],
  };
}

describe("ObservabilityPage", () => {
  const originalSingleTenant = config.singleTenant;

  beforeEach(() => {
    vi.clearAllMocks();
    config.singleTenant = true;

    mockFetch.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes(`${config.apiBaseUrl}/observability/overview`)) {
        const hoursBack = url.includes("hours_back=6") ? 6 : 24;
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(buildOverview(hoursBack)),
        });
      }
      if (url.includes(`${config.apiBaseUrl}/observability/checks`)) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(buildProductChecks()),
        });
      }
      return Promise.resolve({
        ok: false,
        text: () => Promise.resolve("Not found"),
      });
    });
  });

  afterEach(() => {
    config.singleTenant = originalSingleTenant;
  });

  it("renders the built-in observability dashboard", async () => {
    renderObservabilityPage();

    expect(await screen.findByText("Health")).toBeInTheDocument();
    expect(screen.getByLabelText("Health window")).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText("12")).toBeInTheDocument();
    });

    const completedHeading = screen.getByText("Managed Turns");
    const completedCard = completedHeading.closest(".metric-card");
    expect(completedCard).not.toBeNull();
    expect(within(completedCard as Element).getByText("12")).toBeInTheDocument();

    const unhealthyHeading = screen.getByText("Healthy Machines");
    const unhealthyCard = unhealthyHeading.closest(".metric-card");
    expect(unhealthyCard).not.toBeNull();
    expect(within(unhealthyCard as Element).getByText("1/2")).toBeInTheDocument();

    expect(screen.getByText("What the current window says")).toBeInTheDocument();
    expect(screen.getByText("Can users work right now")).toBeInTheDocument();
    expect(screen.getByText("Machine Connected")).toBeInTheDocument();
    expect(screen.getByText("1 recent machine connected and healthy.")).toBeInTheDocument();
    expect(screen.getByText("Render Freshness")).toBeInTheDocument();
    expect(screen.getByText("Render beacons are fresh; latest arrived 10s ago.")).toBeInTheDocument();
    expect(screen.getByText("Live Preview")).toBeInTheDocument();
    expect(screen.getByText("Live preview latency is within threshold.")).toBeInTheDocument();
    expect(screen.getAllByText("15m · full coverage")).toHaveLength(3);
    expect(screen.getByText("Which providers are contributing to the pain")).toBeInTheDocument();
    expect(screen.getByText("Shipping truth from the latest heartbeats")).toBeInTheDocument();
    expect(screen.getByText("The slowest managed turns in this window")).toBeInTheDocument();
    expect(screen.getByText("1 machine blocked or offline")).toBeInTheDocument();
    expect(screen.getByText("Claude is driving most of the slow turns")).toBeInTheDocument();
    expect(screen.getByText("Dispatch looks healthy; slowness is later in the turn")).toBeInTheDocument();
    expect(screen.getAllByText("broken-machine").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Claude").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Codex").length).toBeGreaterThan(0);
    expect(screen.getByText("1 dead-letter range(s) need repair.")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Open machine sessions" })[0]).toHaveAttribute(
      "href",
      "/timeline?device_id=broken-machine",
    );
    expect(screen.getByRole("link", { name: "Open Claude sessions" })).toHaveAttribute(
      "href",
      "/timeline?provider=claude",
    );
    expect(screen.getByRole("link", { name: /Claude · aaaaaaaa/i })).toHaveAttribute(
      "href",
      "/timeline/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    );
  });

  it("refetches when the time window changes", async () => {
    renderObservabilityPage();
    const user = userEvent.setup();

    const completedHeading = await screen.findByText("Managed Turns");
    const completedCard = completedHeading.closest(".metric-card");
    expect(completedCard).not.toBeNull();
    await waitFor(() => {
      expect(within(completedCard as Element).getByText("12")).toBeInTheDocument();
    });

    const select = screen.getByLabelText("Health window") as HTMLSelectElement;
    await user.selectOptions(select, "6");

    await waitFor(() => {
      const updatedHeading = screen.getByText("Managed Turns");
      const updatedCard = updatedHeading.closest(".metric-card");
      expect(updatedCard).not.toBeNull();
      expect(select.value).toBe("6");
      expect(within(updatedCard as Element).getByText("8")).toBeInTheDocument();
      expect(screen.getAllByText("Last 6 Hours").length).toBeGreaterThan(0);
    });
  });

  it("flags elevated dispatch overhead and links to the slow-turn section action", async () => {
    mockFetch.mockImplementationOnce(() => {
      const overview = cloneOverview(24);
      overview.summary.submit_to_send_ms.p95 = 18000;
      overview.summary.total_turn_time_ms.p95 = 40000;
      overview.summary.slow_turns = 3;
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(overview),
      });
    });

    renderObservabilityPage();

    expect(await screen.findByText("Dispatch overhead is elevated")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open slow turns" })).toBeInTheDocument();
  });

  it("shows a no-turns diagnosis when latency data is not available yet", async () => {
    mockFetch.mockImplementationOnce(() => {
      const overview = cloneOverview(24);
      overview.summary.completed_turns = 0;
      overview.summary.slow_turns = 0;
      overview.providers = [];
      overview.machines = [];
      overview.machine_counts = {
        total: 0,
        healthy: 0,
        degraded: 0,
        offline: 0,
        broken: 0,
      };
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(overview),
      });
    });

    renderObservabilityPage();

    expect(await screen.findByText("No completed managed turns in this window")).toBeInTheDocument();
  });

  it("keeps the machine diagnosis ahead of no-turns when shipping is already broken", async () => {
    mockFetch.mockImplementationOnce(() => {
      const overview = cloneOverview(24);
      overview.summary.completed_turns = 0;
      overview.summary.slow_turns = 0;
      overview.providers = [];
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(overview),
      });
    });

    renderObservabilityPage();

    expect(await screen.findByText("1 machine blocked or offline")).toBeInTheDocument();
    expect(screen.queryByText("No completed managed turns in this window")).not.toBeInTheDocument();
  });

  it("falls back to an all-clear diagnosis when the current window is healthy", async () => {
    mockFetch.mockImplementationOnce(() => {
      const overview = cloneOverview(24);
      overview.summary.slow_turns = 0;
      overview.summary.total_turn_time_ms.p95 = 22000;
      overview.summary.total_turn_time_ms.max = 24000;
      overview.providers = [
        {
          ...overview.providers[0],
          slow_turns: 0,
          total_turn_time_ms: { ...overview.providers[0].total_turn_time_ms, p95: 18000 },
        },
      ];
      overview.slow_turns = [];
      overview.slow_turn_total = 0;
      overview.machines = [
        {
          ...overview.machines[1],
          device_id: "healthy-machine",
        },
      ];
      overview.machine_counts = {
        total: 1,
        healthy: 1,
        degraded: 0,
        offline: 0,
        broken: 0,
      };
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(overview),
      });
    });

    renderObservabilityPage();

    expect(await screen.findByText("No active health regressions in this slice")).toBeInTheDocument();
  });

  it("shows a single-tenant note when the page is unavailable", () => {
    config.singleTenant = false;
    renderObservabilityPage();

    expect(screen.getByText("Health is single-tenant for now")).toBeInTheDocument();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("handles API failures cleanly", async () => {
    mockFetch.mockImplementation(() =>
      Promise.resolve({
        ok: false,
        status: 501,
        text: () => Promise.resolve("Single-tenant only"),
      }),
    );

    renderObservabilityPage();

    await waitFor(() => {
      expect(screen.getByText("Error loading health")).toBeInTheDocument();
    });
    expect(screen.getByText("Health is only available on single-tenant runtimes right now.")).toBeInTheDocument();
  });

  it("handles forbidden responses cleanly", async () => {
    mockFetch.mockImplementation(() =>
      Promise.resolve({
        ok: false,
        status: 403,
        text: () => Promise.resolve("Forbidden"),
      }),
    );

    renderObservabilityPage();

    await waitFor(() => {
      expect(screen.getByText("Error loading health")).toBeInTheDocument();
    });
    expect(screen.getByText("You do not have access to this health surface.")).toBeInTheDocument();
  });
});
