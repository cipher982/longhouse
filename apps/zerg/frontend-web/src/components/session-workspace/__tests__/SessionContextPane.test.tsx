import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SessionContextPane } from "../SessionContextPane";
import type { AgentSession, SessionLoopMode } from "../../../services/api/agents";

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
  }: {
    session?: AgentSession;
    onLoopModeChange?: (nextMode: SessionLoopMode) => void;
    loopModePending?: boolean;
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
    />,
  );
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
});
