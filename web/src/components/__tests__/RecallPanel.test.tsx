import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RecallPanel } from "../RecallPanel";

const hookMocks = vi.hoisted(() => ({
  useRecall: vi.fn(),
  useRecallContext: vi.fn(),
  useDebouncedValue: vi.fn(),
}));

vi.mock("../../hooks/useAgentSessions", () => ({
  useRecall: hookMocks.useRecall,
  useRecallContext: hookMocks.useRecallContext,
}));

vi.mock("../../hooks/useDebouncedValue", () => ({
  useDebouncedValue: hookMocks.useDebouncedValue,
}));

describe("RecallPanel", () => {
  beforeEach(() => {
    hookMocks.useDebouncedValue.mockReturnValue("migration");
    hookMocks.useRecallContext.mockImplementation((ref: string | null) => ({
      data: ref ? {
        ref,
        session_id: "11111111-1111-4111-8111-111111111111",
        turns: [{ role: "assistant", content_text: "The full migration context.", is_match: true }],
        total_events: 42,
        content_byte_budget: 6000,
        content_bytes_returned: 27,
        max_content_bytes_applied: 1200,
        evidence_status: "complete",
        evidence_reason: null,
      } : undefined,
      isLoading: false,
      error: null,
    }));
    hookMocks.useRecall.mockReturnValue({
      data: {
        total: 1,
        lanes: ["lexical", "dense"],
        degraded: [],
        coverage: {
          complete: true,
          lagging_sessions: 0,
          unpublished_sessions: 0,
          oldest_lag_seconds: null,
        },
        results: [
          {
            ref: `rr1_${"A".repeat(55)}`,
            session_id: "11111111-1111-4111-8111-111111111111",
            project: "longhouse",
            provider: "codex",
            started_at: "2026-08-02T00:00:00Z",
            snippet: "The migration completed successfully.",
            snippet_unavailable_reason: null,
            matched_by: ["lexical", "dense"],
          },
        ],
      },
      isLoading: false,
      error: null,
    });
  });

  it("renders compact source cards without fetching context", () => {
    render(
      <MemoryRouter>
        <RecallPanel />
      </MemoryRouter>,
    );

    expect(screen.getByText("Lexical + Semantic")).toBeInTheDocument();
    expect(screen.getByText("longhouse · codex")).toBeInTheDocument();
    expect(screen.getByText("The migration completed successfully.")).toBeInTheDocument();
    expect(screen.getByText(/Corpus current/)).toBeInTheDocument();
    expect(hookMocks.useRecallContext).toHaveBeenCalledWith(null);
    expect(screen.queryByText("The full migration context.")).not.toBeInTheDocument();
  });

  it("labels a bounded live head as a snapshot instead of a complete corpus", () => {
    const current = hookMocks.useRecall();
    hookMocks.useRecall.mockReturnValue({
      ...current,
      data: {
        ...current.data,
        coverage: {
          ...current.data.coverage,
          complete: false,
          lagging_sessions: 1,
          oldest_lag_seconds: 1,
        },
      },
    });

    render(
      <MemoryRouter>
        <RecallPanel />
      </MemoryRouter>,
    );

    expect(screen.getByText(/Corpus snapshot · 1 session updating/)).toBeInTheDocument();
    expect(screen.queryByText(/Corpus current/)).not.toBeInTheDocument();
  });

  it("opens context for only the selected result", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <RecallPanel />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Show context" }));

    expect(hookMocks.useRecallContext).toHaveBeenLastCalledWith(`rr1_${"A".repeat(55)}`);
    expect(screen.getByText("The full migration context.")).toBeInTheDocument();
  });

  it("requests only the explicitly selected retrieval mode", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <RecallPanel />
      </MemoryRouter>,
    );

    expect(hookMocks.useRecall).toHaveBeenLastCalledWith(expect.objectContaining({ mode: "auto" }));
    await user.click(screen.getByRole("button", { name: "Lexical" }));

    expect(hookMocks.useRecall).toHaveBeenLastCalledWith(expect.objectContaining({ mode: "lexical" }));
  });
});
