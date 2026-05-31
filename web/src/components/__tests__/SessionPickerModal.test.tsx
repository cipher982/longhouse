import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, beforeEach, vi } from "vitest";
import { SessionPickerModal } from "../SessionPickerModal";

const apiMocks = vi.hoisted(() => ({
  fetchAgentSessionSummaries: vi.fn(),
  fetchAgentSessionPreview: vi.fn(),
  fetchAgentFilters: vi.fn(),
}));

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  return {
    ...actual,
    ...apiMocks,
  };
});

function renderModal(props: Partial<React.ComponentProps<typeof SessionPickerModal>> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  const defaultProps: React.ComponentProps<typeof SessionPickerModal> = {
    isOpen: true,
    onClose: vi.fn(),
    onSelect: vi.fn(),
    ...props,
  };

  return render(
    <QueryClientProvider client={queryClient}>
      <SessionPickerModal {...defaultProps} />
    </QueryClientProvider>,
  );
}

describe("SessionPickerModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();

    apiMocks.fetchAgentSessionSummaries.mockResolvedValue({
      sessions: [
        {
          id: "sess-1",
          project: "zerg",
          provider: "claude",
          cwd: "/Users/example/git/zerg",
          git_repo: null,
          git_branch: null,
          started_at: "2026-03-20T12:00:00Z",
          ended_at: null,
          duration_minutes: 3,
          turn_count: 4,
          last_user_message: "First session",
          last_ai_message: "Reply",
        },
        {
          id: "sess-2",
          project: "zerg",
          provider: "codex",
          cwd: "/Users/example/git/zerg",
          git_repo: null,
          git_branch: null,
          started_at: "2026-03-19T12:00:00Z",
          ended_at: null,
          duration_minutes: 2,
          turn_count: 2,
          last_user_message: "Second session",
          last_ai_message: "Reply",
        },
      ],
      total: 2,
    });
    apiMocks.fetchAgentSessionPreview.mockResolvedValue({
      messages: [{ role: "user", content: "Preview", timestamp: "2026-03-20T12:00:00Z" }],
      total_messages: 1,
    });
    apiMocks.fetchAgentFilters.mockResolvedValue({
      projects: ["zerg"],
      providers: ["claude", "codex"],
      machines: [],
    });
  });

  it("handles null initialFilters without crashing", () => {
    expect(() => renderModal({ initialFilters: null as unknown as never })).not.toThrow();
  });

  it("resets the query draft when the modal closes and reopens", async () => {
    const user = userEvent.setup();
    const view = renderModal({
      initialFilters: {
        query: "from server",
      },
    });

    const searchInput = await screen.findByPlaceholderText("Search sessions...");
    expect(searchInput).toHaveValue("from server");

    await user.clear(searchInput);
    await user.type(searchInput, "edited");
    expect(searchInput).toHaveValue("edited");

    view.rerender(
      <QueryClientProvider
        client={
          new QueryClient({
            defaultOptions: {
              queries: { retry: false },
            },
          })
        }
      >
        <SessionPickerModal
          isOpen={false}
          initialFilters={{ query: "from server" }}
          onClose={vi.fn()}
          onSelect={vi.fn()}
        />
      </QueryClientProvider>,
    );

    view.rerender(
      <QueryClientProvider
        client={
          new QueryClient({
            defaultOptions: {
              queries: { retry: false },
            },
          })
        }
      >
        <SessionPickerModal
          isOpen={true}
          initialFilters={{ query: "from server" }}
          onClose={vi.fn()}
          onSelect={vi.fn()}
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByPlaceholderText("Search sessions...")).toHaveValue("from server");
  });

  it("resumes the first visible session when no explicit selection was made", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();

    renderModal({ onSelect });

    await screen.findByText("First session");
    await user.click(screen.getByRole("button", { name: "Resume" }));

    await waitFor(() => expect(onSelect).toHaveBeenCalledWith("sess-1"));
  });
});
