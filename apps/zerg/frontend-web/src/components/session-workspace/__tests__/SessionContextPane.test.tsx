import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SessionContextPane } from "../SessionContextPane";
import type { AgentSession, SessionLoopMode } from "../../../services/api/agents";
import type { SessionShadowReview } from "../../../services/api/oikos";

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
    latestShadowReview = null,
    shadowReviewLoading = false,
    shadowReviewUnavailable = false,
  }: {
    session?: AgentSession;
    onLoopModeChange?: (nextMode: SessionLoopMode) => void;
    loopModePending?: boolean;
    latestShadowReview?: SessionShadowReview | null;
    shadowReviewLoading?: boolean;
    shadowReviewUnavailable?: boolean;
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
      latestShadowReview={latestShadowReview}
      shadowReviewLoading={shadowReviewLoading}
      shadowReviewUnavailable={shadowReviewUnavailable}
    />,
  );
}

function makeShadowReview(overrides: Partial<SessionShadowReview> = {}): SessionShadowReview {
  return {
    generatedAt: "2026-03-17T11:00:00Z",
    triggerType: "presence.blocked",
    decision: "suggest_continue",
    summary: "Ask the user whether Oikos should continue the bounded follow-up.",
    rationale: "The session has one bounded next step but still needs approval.",
    needsHuman: true,
    loopMode: "assist",
    modeCapability: "notify_only",
    modeSummary: "Suggest bounded next steps or escalations, but wait for user approval before continuing.",
    executionState: "awaiting_user_approval",
    wouldNotifyUser: true,
    wouldContinueSession: false,
    blockedReasons: ["Waiting on direct approval before resuming the session."],
    recommendedAction: "continue_session",
    wakeupStatus: "ignored",
    wakeupReason: "no_action",
    actualOutcome: "ignore",
    expectedOutcome: "notify_user",
    alignment: "more_conservative",
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

  it("renders the latest shadow review details", () => {
    renderPane({ latestShadowReview: makeShadowReview() });

    expect(screen.getByTestId("session-shadow-review")).toBeInTheDocument();
    expect(screen.getByText(/Awaiting Approval/i)).toBeInTheDocument();
    expect(screen.getByText(/More Conservative/i)).toBeInTheDocument();
    expect(screen.getByText(/Trigger: presence.blocked/i)).toBeInTheDocument();
    expect(screen.getByText(/Recommended action: Continue Session/i)).toBeInTheDocument();
    expect(screen.getByText(/Shadow expected outcome: Notify User/i)).toBeInTheDocument();
    expect(screen.getByText(/Actual outcome: Ignore/i)).toBeInTheDocument();
    expect(screen.getByText(/Waiting on direct approval/i)).toBeInTheDocument();
  });

  it("shows a graceful empty state when no shadow review is available", () => {
    renderPane({ shadowReviewUnavailable: true });

    expect(screen.getByText(/Shadow review is unavailable right now/i)).toBeInTheDocument();
  });
});
