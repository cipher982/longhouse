import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SessionContextPane } from "../SessionContextPane";
import type { AgentSession, SessionLoopMode } from "../../../services/api/agents";
import type { SessionTurnReview } from "../../../services/api/oikos";

function makeSession(overrides: Partial<AgentSession> = {}): AgentSession {
  return {
    id: "sess-1",
    provider: "claude",
    project: "zerg",
    device_id: "cinder",
    environment: "development",
    cwd: "/Users/davidrose/git/zerg",
    git_repo: "git@github.com:cipher982/longhouse.git",
    git_branch: "main",
    started_at: "2026-03-17T10:00:00Z",
    ended_at: null,
    last_activity_at: "2026-03-17T10:05:00Z",
    user_messages: 3,
    assistant_messages: 4,
    tool_calls: 2,
    summary: "Only targeted verification remains.",
    summary_title: "Loop mode test",
    first_user_message: "Keep going.",
    thread_root_session_id: "sess-1",
    thread_head_session_id: "sess-1",
    thread_continuation_count: 1,
    continued_from_session_id: null,
    continuation_kind: "local",
    origin_label: "Cinder",
    branched_from_event_id: null,
    is_writable_head: true,
    loop_mode: "assist",
    ...overrides,
  };
}

function renderPane(
  {
    session = makeSession(),
    onLoopModeChange = vi.fn(),
    loopModePending = false,
    latestTurnReview = null,
    turnReviewLoading = false,
    turnReviewUnavailable = false,
  }: {
    session?: AgentSession;
    onLoopModeChange?: (nextMode: SessionLoopMode) => void;
    loopModePending?: boolean;
    latestTurnReview?: SessionTurnReview | null;
    turnReviewLoading?: boolean;
    turnReviewUnavailable?: boolean;
  } = {},
) {
  return render(
    <SessionContextPane
      session={session}
      title="Loop mode test"
      headThreadSession={session}
      threadSessions={[session]}
      isViewingHead
      onOpenSession={vi.fn()}
      onOpenLatest={vi.fn()}
      onLoopModeChange={onLoopModeChange}
      loopModePending={loopModePending}
      latestTurnReview={latestTurnReview}
      turnReviewLoading={turnReviewLoading}
      turnReviewUnavailable={turnReviewUnavailable}
    />,
  );
}

function makeTurnReview(overrides: Partial<SessionTurnReview> = {}): SessionTurnReview {
  return {
    id: 1,
    sessionId: "sess-1",
    assistantEventId: 77,
    turnIndex: 6,
    triggerType: "turn.completed",
    loopMode: "assist",
    decision: "continue",
    summary: "The turn left one obvious bounded next step ready to continue.",
    rationale: "This looks like the routine continue case.",
    turnExcerpt: "Only targeted verification remains. Run the pending targeted tests.",
    modeCapability: "notify_only",
    modeSummary: "Suggest or escalate from completed turns, but wait for approval before continuing.",
    executionState: "awaiting_user_approval",
    recommendedAction: "continue_session",
    followUpPrompt: "Run the pending targeted tests.",
    blockedReasons: ["Autonomous continue cap reached."],
    status: "recorded",
    reason: null,
    runId: 123,
    actualOutcome: "ignore",
    alignment: "more_conservative",
    createdAt: "2026-03-17T11:00:00Z",
    ...overrides,
  };
}

describe("SessionContextPane", () => {
  it("shows the current loop mode as the active radio option", () => {
    renderPane({ session: makeSession({ loop_mode: "assist" }) });

    expect(screen.getByRole("radio", { name: /assist/i })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: /manual/i })).toHaveAttribute("aria-checked", "false");
  });

  it("calls back with the selected loop mode", async () => {
    const user = userEvent.setup();
    const onLoopModeChange = vi.fn();

    renderPane({ onLoopModeChange });
    await user.click(screen.getByRole("radio", { name: /autopilot/i }));

    expect(onLoopModeChange).toHaveBeenCalledWith("autopilot");
  });

  it("disables the loop mode controls while an update is pending", () => {
    renderPane({ loopModePending: true });

    for (const label of ["Manual", "Assist", "Autopilot"]) {
      expect(screen.getByRole("radio", { name: new RegExp(label, "i") })).toBeDisabled();
    }
  });

  it("renders the latest completed-turn review details", () => {
    renderPane({ latestTurnReview: makeTurnReview() });

    const turnReview = screen.getByTestId("session-turn-review");
    expect(turnReview).toBeInTheDocument();
    expect(within(turnReview).getByText(/^Continue$/i)).toBeInTheDocument();
    expect(within(turnReview).getByText(/^Ask You$/i)).toBeInTheDocument();
    expect(within(turnReview).getByText(/Latest assistant turn #7/i)).toBeInTheDocument();
    expect(within(turnReview).getByText(/Recommended action: Continue Session/i)).toBeInTheDocument();
    expect(within(turnReview).getByText(/Suggested next prompt: Run the pending targeted tests\./i)).toBeInTheDocument();
    expect(within(turnReview).getByText(/Live outcome: Ignore/i)).toBeInTheDocument();
    expect(within(turnReview).getByText(/Only targeted verification remains\. Run the pending targeted tests\./i)).toBeInTheDocument();
    expect(within(turnReview).getByText(/Autonomous continue cap reached/i)).toBeInTheDocument();
  });

  it("shows a graceful empty state when no turn review is available", () => {
    renderPane({ turnReviewUnavailable: true });

    expect(screen.getByText(/Turn-loop review is unavailable right now/i)).toBeInTheDocument();
  });
});
