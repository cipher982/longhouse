import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import IntegrationsPage from "../IntegrationsPage";
import config from "../../lib/config";

// Force cleanup after each test to prevent DOM accumulation
afterEach(() => {
  cleanup();
});

// Mock connector data matches AccountConnectorStatus interface
const mockConnectors = [
  {
    type: "slack",
    name: "Slack",
    description: "Send messages to Slack channels",
    category: "notifications",
    icon: "slack",
    docs_url: "https://slack.com/docs",
    fields: [
      { key: "webhook_url", label: "Webhook URL", type: "text", placeholder: "https://...", required: true }
    ],
    configured: true,
    display_name: "My Slack",
    test_status: "success",
    last_tested_at: "2024-01-01T00:00:00Z",
    metadata: { workspace: "TestWorkspace" },
  },
  {
    type: "github",
    name: "GitHub",
    description: "Interact with GitHub repositories",
    category: "project_management",
    icon: "github",
    docs_url: "https://github.com/docs",
    fields: [
      { key: "token", label: "Personal Access Token", type: "password", placeholder: "ghp_...", required: true }
    ],
    configured: false,
    display_name: null,
    test_status: "untested",
    last_tested_at: null,
    metadata: null,
  },
];

// Mock fetch for API calls
const mockFetch = vi.fn();
global.fetch = mockFetch;

function renderIntegrationsPage() {
  // Create a unique QueryClient for each test to prevent cache leakage
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <IntegrationsPage />
    </QueryClientProvider>
  );
}

describe("IntegrationsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();

    // Default mock for fetching connectors
    mockFetch.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();

      if (url.includes("/account/connectors")) {
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: {
            get: (key: string) => key.toLowerCase() === 'content-type' ? 'application/json' : null
          },
          json: () => Promise.resolve(mockConnectors),
        } as unknown as Response);
      }
      return Promise.resolve({
        ok: false,
        status: 404,
        headers: { get: () => null },
        text: () => Promise.resolve("Not found"),
      } as unknown as Response);
    });
  });

  it("renders the page title and description", async () => {
    renderIntegrationsPage();
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Integrations" })).toBeInTheDocument();
    });
    expect(screen.getByText(/Configure credentials for external services/)).toBeInTheDocument();
  });

  it("displays configured and unconfigured connectors", async () => {
    renderIntegrationsPage();

    await waitFor(() => {
      expect(screen.getByText("My Slack")).toBeInTheDocument();
    });

    // Slack should show "Connected"
    const slackCard = screen.getByText("My Slack").closest(".connector-card");
    expect(slackCard).not.toBeNull();
    expect(within(slackCard as HTMLElement).getByText("Connected")).toBeInTheDocument();

    // GitHub should show "Not configured"
    expect(screen.getByText("GitHub")).toBeInTheDocument();
    const githubCard = screen.getByText("GitHub").closest(".connector-card");
    expect(githubCard).not.toBeNull();
    expect(within(githubCard as HTMLElement).getByText("Not configured")).toBeInTheDocument();
  });

  it("opens configuration modal when Configure button is clicked", async () => {
    renderIntegrationsPage();
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByText("GitHub")).toBeInTheDocument();
    });

    // Find and click "Configure" on GitHub card
    const githubCard = screen.getByText("GitHub").closest(".connector-card");
    const configureBtn = within(githubCard as HTMLElement).getByRole("button", { name: "Configure" });
    await user.click(configureBtn);

    // Modal should appear
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Configure GitHub" })).toBeInTheDocument();
    expect(screen.getByLabelText("Personal Access Token")).toBeInTheDocument();
  });

  it("shows error message when API fails", async () => {
    // Mock API failure
    mockFetch.mockImplementationOnce(() =>
      Promise.resolve({
        ok: false,
        status: 500,
        headers: { get: () => null },
        text: () => Promise.resolve("Internal Server Error"),
      } as unknown as Response)
    );

    renderIntegrationsPage();

    await waitFor(() => {
      expect(screen.getByText(/Failed to load integrations/)).toBeInTheDocument();
    });
  });
});

