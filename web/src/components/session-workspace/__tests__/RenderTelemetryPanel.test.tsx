import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RenderTelemetryPanel } from "../RenderTelemetryPanel";
import { fetchRecentClientRenderBeacons } from "../../../services/api/telemetry";

vi.mock("../../../services/api/telemetry", () => ({
  fetchRecentClientRenderBeacons: vi.fn(),
}));

const fetchRecentClientRenderBeaconsMock = vi.mocked(fetchRecentClientRenderBeacons);

function renderPanel() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <RenderTelemetryPanel sessionId="session-codex" />
    </QueryClientProvider>,
  );
}

describe("RenderTelemetryPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("does not fetch until the user asks for recent beacons", async () => {
    fetchRecentClientRenderBeaconsMock.mockResolvedValue({
      items: [
        {
          session_id: "session-codex",
          event_id: "42",
          surface: "ios",
          managed: true,
          latency_ms: 124,
          emitted_at_ms: 1_769_482_800_000,
          rendered_at_ms: 1_769_482_800_124,
          clock_skew_ms: -8,
          webkit: {
            stage: "rendered",
            payload_byte_size: 4096,
            row_count: 18,
            latest_item_id: "assistant:42",
            render_sequence: 3,
            js_failure_count: 0,
            should_stick_to_bottom: true,
            web_view_loaded: true,
          },
          observed_at: "2026-01-22T19:00:00Z",
          received_at: "2026-01-22T19:00:01Z",
        },
      ],
    });

    renderPanel();

    expect(fetchRecentClientRenderBeaconsMock).not.toHaveBeenCalled();
    expect(screen.getByText("Load")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Load" }));

    await waitFor(() => {
      expect(fetchRecentClientRenderBeaconsMock).toHaveBeenCalledWith({
        sessionId: "session-codex",
        limit: 8,
      });
    });
    expect(await screen.findByText("event 42")).toBeInTheDocument();
    expect(screen.getByText("ios")).toBeInTheDocument();
    expect(screen.getByText("124ms")).toBeInTheDocument();
    expect(screen.getByText("-8ms")).toBeInTheDocument();
    expect(screen.getByText("WebKit")).toBeInTheDocument();
    expect(screen.getByText("4096B")).toBeInTheDocument();
    expect(screen.getByText("assistant:42")).toBeInTheDocument();
  });

  it("shows an empty state after a successful empty fetch", async () => {
    fetchRecentClientRenderBeaconsMock.mockResolvedValue({ items: [] });

    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Load" }));

    expect(
      await screen.findByText("No render beacons recorded yet."),
    ).toBeInTheDocument();
  });
});
