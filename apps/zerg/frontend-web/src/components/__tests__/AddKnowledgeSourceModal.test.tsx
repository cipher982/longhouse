import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AddKnowledgeSourceModal } from "../AddKnowledgeSourceModal";

const apiMocks = vi.hoisted(() => ({
  fetchGitHubRepos: vi.fn(),
  fetchGitHubBranches: vi.fn(),
  createKnowledgeSource: vi.fn(),
}));

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  return {
    ...actual,
    ...apiMocks,
  };
});

vi.mock("react-hot-toast", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

function renderModal(isOpen: boolean = true) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <AddKnowledgeSourceModal isOpen={isOpen} onClose={vi.fn()} />
    </QueryClientProvider>,
  );
}

describe("AddKnowledgeSourceModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();

    apiMocks.fetchGitHubRepos.mockImplementation((page: number) => {
      if (page === 1) {
        return Promise.resolve({
          repositories: [
            {
              id: 1,
              full_name: "david/repo-one",
              owner: "david",
              name: "repo-one",
              private: false,
              default_branch: "main",
              description: "First repo",
              updated_at: "2026-03-20T12:00:00Z",
            },
          ],
          page: 1,
          per_page: 30,
          has_more: true,
        });
      }

      return Promise.resolve({
        repositories: [
          {
            id: 1,
            full_name: "david/repo-one",
            owner: "david",
            name: "repo-one",
            private: false,
            default_branch: "main",
            description: "First repo",
            updated_at: "2026-03-20T12:00:00Z",
          },
          {
            id: 2,
            full_name: "david/repo-two",
            owner: "david",
            name: "repo-two",
            private: true,
            default_branch: "main",
            description: "Second repo",
            updated_at: "2026-03-20T12:00:00Z",
          },
        ],
        page: 2,
        per_page: 30,
        has_more: false,
      });
    });
    apiMocks.fetchGitHubBranches.mockResolvedValue({
      branches: [{ name: "main", protected: false, is_default: true }],
    });
    apiMocks.createKnowledgeSource.mockResolvedValue({
      id: 1,
      name: "repo-one",
      source_type: "github_repo",
      config: {},
      status: "ready",
      last_synced_at: null,
      created_at: "2026-03-20T12:00:00Z",
      updated_at: "2026-03-20T12:00:00Z",
      sync_status: "idle",
      sync_error: null,
    });
  });

  it("resets modal state when it closes and reopens", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });

    const view = render(
      <QueryClientProvider client={queryClient}>
        <AddKnowledgeSourceModal isOpen={true} onClose={vi.fn()} />
      </QueryClientProvider>,
    );

    await user.click(screen.getByTestId("source-type-url"));
    const urlInput = await screen.findByTestId("url-input");
    await user.type(urlInput, "https://example.com/docs");
    expect(urlInput).toHaveValue("https://example.com/docs");

    view.rerender(
      <QueryClientProvider client={queryClient}>
        <AddKnowledgeSourceModal isOpen={false} onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    expect(screen.queryByTestId("url-input")).toBeNull();

    view.rerender(
      <QueryClientProvider client={queryClient}>
        <AddKnowledgeSourceModal isOpen={true} onClose={vi.fn()} />
      </QueryClientProvider>,
    );

    await user.click(await screen.findByTestId("source-type-url"));
    expect(await screen.findByTestId("url-input")).toHaveValue("");
  });

  it("loads additional GitHub pages without duplicating repositories", async () => {
    const user = userEvent.setup();

    renderModal();

    await user.click(screen.getByRole("button", { name: /GitHub Repository/ }));

    expect(await screen.findByText("david/repo-one")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load More" }));

    await waitFor(() => {
      expect(screen.getByText("david/repo-two")).toBeInTheDocument();
    });
    expect(screen.getAllByText("david/repo-one")).toHaveLength(1);
  });
});
