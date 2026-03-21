import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SwarmOpsPage from "../SwarmOpsPage";
import { request } from "../../services/api";
import { TestRouter } from "../../test/test-utils";

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  return {
    ...actual,
    request: vi.fn(),
  };
});

const requestMock = request as unknown as vi.MockedFunction<typeof request>;

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="swarm-location">{`${location.pathname}${location.search}`}</div>;
}

function SwarmOpsRoute() {
  return (
    <>
      <SwarmOpsPage />
      <LocationProbe />
    </>
  );
}

function renderSwarmOps(initialEntry = "/runs") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, cacheTime: 0 },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <TestRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/runs" element={<SwarmOpsRoute />} />
          <Route path="/automations/:automationId/thread/:threadId" element={<LocationProbe />} />
        </Routes>
      </TestRouter>
    </QueryClientProvider>
  );
}

describe("SwarmOpsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    if (typeof window !== "undefined") {
      window.sessionStorage.clear();
    }

    requestMock.mockImplementation((path: string) => {
      if (path === "/oikos/runs?limit=120") {
        return Promise.resolve([
          {
            id: 7,
            automation_id: 101,
            thread_id: 501,
            automation_name: "Priority Inbox",
            status: "running",
            summary: "Need your input",
            signal: "Need your input",
            signal_source: "summary",
            created_at: "2026-03-17T12:05:00Z",
            updated_at: "2026-03-17T12:05:00Z",
            completed_at: null,
          },
          {
            id: 8,
            automation_id: 102,
            thread_id: 502,
            automation_name: "Archive Sweep",
            status: "success",
            summary: "Completed cleanly",
            signal: "Completed cleanly",
            signal_source: "summary",
            created_at: "2026-03-16T10:00:00Z",
            updated_at: "2026-03-16T10:10:00Z",
            completed_at: "2026-03-16T10:10:00Z",
          },
        ]);
      }

      if (path === "/oikos/runs/7/events?limit=120") {
        return Promise.resolve({
          run_id: 7,
          events: [],
          total: 0,
        });
      }

      if (path === "/oikos/runs/8/events?limit=120") {
        return Promise.resolve({
          run_id: 8,
          events: [],
          total: 0,
        });
      }

      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });
  });

  it("canonicalizes the base runs route to the first visible run", async () => {
    renderSwarmOps();

    await waitFor(() => {
      expect(screen.getByTestId("swarm-location")).toHaveTextContent("/runs?run=7");
    });
  });

  it("makes filter and selected run URL-owned", async () => {
    renderSwarmOps("/runs?filter=all&run=7");

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Completed" })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Completed" }));

    await waitFor(() => {
      expect(screen.getByTestId("swarm-location")).toHaveTextContent("/runs?filter=done&run=8");
    });

    expect((await screen.findAllByText("Archive Sweep")).length).toBeGreaterThan(0);
  });

  it("uses the selected run from the URL to render the detail pane", async () => {
    renderSwarmOps("/runs?run=7");

    await waitFor(() => {
      expect(screen.getByTestId("swarm-location")).toHaveTextContent("/runs?run=7");
    });

    expect((await screen.findAllByText("Priority Inbox")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Open thread" })).toBeEnabled();
  });

  it("seeds demo routes through query ownership before loading runs", async () => {
    requestMock.mockImplementation((path: string) => {
      if (path === "/admin/seed-scenario") {
        return Promise.resolve({ ok: true });
      }

      if (path === "/oikos/runs?limit=120") {
        return Promise.resolve([
          {
            id: 7,
            automation_id: 101,
            thread_id: 501,
            automation_name: "Priority Inbox",
            status: "running",
            summary: "Need your input",
            signal: "Need your input",
            signal_source: "summary",
            created_at: "2026-03-17T12:05:00Z",
            updated_at: "2026-03-17T12:05:00Z",
            completed_at: null,
          },
        ]);
      }

      if (path === "/oikos/runs/7/events?limit=120") {
        return Promise.resolve({
          run_id: 7,
          events: [],
          total: 0,
        });
      }

      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    renderSwarmOps("/runs?demo=nightly-demo");

    await waitFor(() => {
      expect(requestMock).toHaveBeenNthCalledWith(
        1,
        "/admin/seed-scenario",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ name: "nightly-demo", clean: true }),
        }),
      );
    });

    await waitFor(() => {
      expect(requestMock).toHaveBeenCalledWith("/oikos/runs?limit=120");
    });

    expect(window.sessionStorage.getItem("swarm-demo-seeded:nightly-demo")).toBe("1");
  });
});
