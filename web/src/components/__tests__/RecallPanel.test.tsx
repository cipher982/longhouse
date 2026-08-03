import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RecallPanel } from "../RecallPanel";

const hookMocks = vi.hoisted(() => ({
  useRecall: vi.fn(),
  useDebouncedValue: vi.fn(),
}));

vi.mock("../../hooks/useAgentSessions", () => ({
  useRecall: hookMocks.useRecall,
}));

vi.mock("../../hooks/useDebouncedValue", () => ({
  useDebouncedValue: hookMocks.useDebouncedValue,
}));

describe("RecallPanel", () => {
  beforeEach(() => {
    hookMocks.useDebouncedValue.mockReturnValue("migration");
    hookMocks.useRecall.mockReturnValue({
      data: {
        total: 1,
        lanes: ["lexical", "dense"],
        embedding_model: "google/embeddinggemma-300m",
        embedding_dims: 256,
        embedding_revision: "a".repeat(40),
        coverage: {
          ready: true,
          projector: "embeddings-test-256d-p2",
          catalog_lag_count: 0,
          catalog_indexed_through: "10",
          catalog_commit_seq: "10",
          catalog_observed_at: "2026-08-02T00:00:00+00:00",
          expected_sessions: 5_901,
          published_sessions: 5_901,
          expected_episodes: 82_958,
          current_episodes: 82_958,
          invalid_vectors: 0,
          unnormalized_vectors: 0,
          unlocatable_episodes: 0,
          episode_count_mismatches: 0,
          missing_session_ids: [],
        },
        matches: [
          {
            session_id: "11111111-1111-4111-8111-111111111111",
            chunk_index: 2,
            score: 0.0325,
            evidence: "The migration completed successfully.",
            retrieval_lanes: ["lexical", "dense"],
            lane_ranks: { lexical: 2, dense: 1 },
            event_index_start: 4,
            event_index_end: 5,
            total_events: 42,
            match_event_id: 99,
            evidence_status: "complete",
            evidence_reason: null,
            context: [
              {
                search_event_id: 99,
                event_id: "event-99",
                source_object_id: "a".repeat(64),
                record_ordinal: 4,
                order_time_us: 123,
                role: "assistant",
                content_text: "The migration completed successfully.",
                tool_name: null,
              },
            ],
          },
        ],
      },
      isLoading: false,
      error: null,
    });
  });

  it("renders lane ranks and real context fields instead of a fake score percentage", () => {
    render(
      <MemoryRouter>
        <RecallPanel />
      </MemoryRouter>,
    );

    expect(screen.getByText("Semantic #1 + Lexical #2")).toBeInTheDocument();
    expect(screen.getByText("The migration completed successfully.")).toBeInTheDocument();
    expect(screen.getByText(/Complete corpus: 82,958 episodes/)).toBeInTheDocument();
    expect(screen.queryByText("3%")).not.toBeInTheDocument();
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
