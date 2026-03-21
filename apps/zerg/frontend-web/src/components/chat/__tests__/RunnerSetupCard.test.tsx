import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RunnerSetupCard } from "../RunnerSetupCard";

const apiMocks = vi.hoisted(() => ({
  fetchRunners: vi.fn(),
}));

vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  return {
    ...actual,
    ...apiMocks,
  };
});

function renderCard() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  render(
    <QueryClientProvider client={queryClient}>
      <RunnerSetupCard
        data={{
          enroll_token: "token-123",
          expires_at: "2100-03-21T22:10:00Z",
          longhouse_url: "https://longhouse.ai",
          docker_command: "docker run runner",
          one_liner_install_command: "curl https://longhouse.ai/install.sh | bash",
        }}
      />
    </QueryClientProvider>,
  );

  return queryClient;
}

describe("RunnerSetupCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("promotes a newly enrolled runner from the polling query", async () => {
    const now = Date.now();
    apiMocks.fetchRunners
      .mockResolvedValueOnce([
        {
          id: 1,
          name: "Existing Runner",
          status: "online",
          created_at: new Date(now - 60_000).toISOString(),
          capabilities: ["exec.readonly"],
        },
      ])
      .mockResolvedValueOnce([
        {
          id: 1,
          name: "Existing Runner",
          status: "online",
          created_at: new Date(now - 60_000).toISOString(),
          capabilities: ["exec.readonly"],
        },
        {
          id: 2,
          name: "Fresh Runner",
          status: "online",
          created_at: new Date(now + 60_000).toISOString(),
          capabilities: ["exec.readonly"],
        },
      ]);

    const queryClient = renderCard();

    expect(await screen.findByText("Waiting for connection...")).toBeInTheDocument();
    await waitFor(() => expect(apiMocks.fetchRunners).toHaveBeenCalledTimes(1));

    await queryClient.refetchQueries({
      queryKey: ["runner-setup-enrollment", "token-123", "2100-03-21T22:10:00Z"],
    });
    await waitFor(() => expect(apiMocks.fetchRunners).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText("Runner Connected!")).toBeInTheDocument());
    expect(screen.getByText(/Fresh Runner/)).toBeInTheDocument();
  });
});
