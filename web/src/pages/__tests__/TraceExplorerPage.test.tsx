import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TestRouter } from "../../test/test-utils";
import TraceExplorerPage from "../TraceExplorerPage";

const authMocks = vi.hoisted(() => ({
  useAuth: vi.fn(),
}));

vi.mock("../../lib/auth", () => ({
  useAuth: authMocks.useAuth,
}));

const { useAuth: mockUseAuth } = authMocks;

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="trace-location">{location.pathname}</div>;
}

function TraceExplorerRoute() {
  return (
    <>
      <TraceExplorerPage />
      <LocationProbe />
    </>
  );
}

function renderTraceExplorer(initialEntry = "/traces") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <TestRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/traces" element={<TraceExplorerRoute />} />
          <Route path="/traces/:traceId" element={<TraceExplorerRoute />} />
        </Routes>
      </TestRouter>
    </QueryClientProvider>,
  );
}

describe("TraceExplorerPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseAuth.mockReturnValue({
      user: {
        id: 1,
        email: "owner@example.com",
      },
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);

        if (url.includes("/traces/?limit=20&offset=0")) {
          return new Response(
            JSON.stringify({
              traces: [
                {
                  trace_id: "trace-run-1",
                  run_id: 11,
                  status: "success",
                  model: "gpt-5.4",
                  started_at: "2026-03-17T12:05:00Z",
                  duration_ms: 5200,
                },
                {
                  trace_id: "trace-run-2",
                  run_id: 12,
                  status: "failed",
                  model: "gpt-5.4",
                  started_at: "2026-03-17T11:00:00Z",
                  duration_ms: 3100,
                },
              ],
              limit: 20,
              offset: 0,
              count: 2,
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          );
        }

        if (url.includes("/traces/trace-run-1?level=summary")) {
          return new Response(
            JSON.stringify({
              trace_id: "trace-run-1",
              status: "SUCCESS",
              started_at: "2026-03-17T12:05:00Z",
              duration_seconds: 5.2,
              counts: {
                runs: 1,
                commis: 1,
                llm_calls: 2,
              },
              anomalies: [],
              timeline: [
                {
                  timestamp: "2026-03-17T12:05:01Z",
                  event_type: "llm_call",
                  source: "llm",
                  details: { model: "gpt-5.4" },
                  is_error: false,
                  duration_ms: 321,
                },
              ],
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          );
        }

        return new Response("not found", { status: 404 });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("uses the trace route as the selected detail source of truth", async () => {
    renderTraceExplorer("/traces/trace-run-1");

    await waitFor(() => {
      expect(screen.getByTestId("trace-location")).toHaveTextContent("/traces/trace-run-1");
    });

    expect(await screen.findByText(/Timeline \(1 events\)/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
  });

  it("renders the trace list on the base route", async () => {
    renderTraceExplorer();

    expect(await screen.findByTestId("trace-row-trace-run-1")).toBeInTheDocument();
    expect(screen.getByTestId("trace-location")).toHaveTextContent("/traces");
  });
});
