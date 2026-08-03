import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import ProviderCapabilitiesPage from "../ProviderCapabilitiesPage";
import config from "../../lib/config";

const mockFetch = vi.fn();
global.fetch = mockFetch;

function buildProjection() {
  return {
    schema_version: 1,
    artifact_kind: "provider_capability_projection",
    capabilities: [
      {
        provider: "codex",
        capability: "coordination.awareness.create",
        assertion_id: "coordination_instructions_model_visible",
        variant: null,
        scenario_id: "codex_coordination_awareness_create",
        declared: true,
        proof_status: "pass",
        generated_at: "2026-07-29T04:00:00+00:00",
        evidence_class: "live_token",
      },
      {
        provider: "cursor",
        capability: "coordination.awareness.create",
        assertion_id: "coordination_instructions_model_visible",
        variant: null,
        scenario_id: "cursor_coordination_awareness_create",
        declared: true,
        proof_status: "never_proven",
        generated_at: null,
        evidence_class: null,
      },
    ],
  };
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ProviderCapabilitiesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("ProviderCapabilitiesPage", () => {
  const originalSingleTenant = config.singleTenant;

  beforeEach(() => {
    vi.clearAllMocks();
    config.singleTenant = true;
  });

  afterEach(() => {
    config.singleTenant = originalSingleTenant;
  });

  it("renders every declared capability row with its proof status", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(buildProjection()),
    });

    renderPage();

    expect(await screen.findByText("Provider capabilities")).toBeInTheDocument();
    await waitFor(() => {
      // Both fixture rows share this assertion_id (codex and cursor both
      // declare it, mirroring the real not-globally-unique case) -- two rows.
      expect(screen.getAllByText("coordination_instructions_model_visible")).toHaveLength(2);
    });
    expect(screen.getByText("pass")).toBeInTheDocument();
    expect(screen.getByText("never_proven")).toBeInTheDocument();
    expect(screen.getByText("1 of 2 declared assertions have a passing proof on record.")).toBeInTheDocument();
    expect(String(mockFetch.mock.calls[0][0])).toContain(`${config.apiBaseUrl}/admin/provider-capabilities`);
  });

  it("shows an error state when the request is forbidden", async () => {
    mockFetch.mockResolvedValue({ ok: false, status: 403 });

    renderPage();

    expect(await screen.findByText("Unable to load provider capabilities")).toBeInTheDocument();
    expect(screen.getByText("Admin access required")).toBeInTheDocument();
  });

  it("still fetches when config.singleTenant is false", async () => {
    // Regression lock (review 2026-07-29, Grok): the page originally copied
    // ObservabilityPage's `enabled: config.singleTenant` query gate, which
    // is correct for that page's genuinely single-tenant-only health data
    // but wrong here -- this data is admin-gated, not tenant-scoped. The
    // gate silently disabled the query outside single-tenant deployments
    // (dev-demo included) and surfaced a misleading "Unknown error" instead
    // of loading. Every other test in this file runs with singleTenant=true
    // from beforeEach, so none of them would have caught a regression here.
    config.singleTenant = false;
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(buildProjection()),
    });

    renderPage();

    expect(await screen.findByText("Provider capabilities")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByText("coordination_instructions_model_visible")).toHaveLength(2);
    });
    expect(screen.queryByText("Unable to load provider capabilities")).not.toBeInTheDocument();
  });
});
