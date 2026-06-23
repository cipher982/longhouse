import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SessionPauseRequestPanel } from "../SessionPauseRequestPanel";
import type { SessionPauseRequest } from "../../../services/api/agents";

function permissionPrompt(overrides: Partial<SessionPauseRequest> = {}): SessionPauseRequest {
  return {
    id: "pause-1",
    session_id: "11111111-1111-1111-1111-111111111111",
    runtime_key: "claude:sess",
    kind: "permission_prompt",
    status: "pending",
    provider: "claude",
    can_respond: true,
    title: "Permission: Bash",
    summary: "Claude wants to use Bash.",
    tool_name: "Bash",
    questions: [],
    occurred_at: null,
    last_seen_at: null,
    resolved_at: null,
    expires_at: null,
    ...overrides,
  } as SessionPauseRequest;
}

describe("SessionPauseRequestPanel — permission prompt", () => {
  it("renders Allow/Deny and a Permission eyebrow, no free-text answer", () => {
    render(<SessionPauseRequestPanel pauseRequest={permissionPrompt()} onRespond={vi.fn()} />);
    expect(screen.getByText("Permission")).toBeInTheDocument();
    expect(screen.getByText("Permission: Bash")).toBeInTheDocument();
    expect(screen.getByText("Allow")).toBeInTheDocument();
    expect(screen.getByText("Deny")).toBeInTheDocument();
    // No free-text answer box for a pure allow/deny prompt.
    expect(screen.queryByLabelText("Answer")).not.toBeInTheDocument();
  });

  it("Allow submits decision=answer", async () => {
    const onRespond = vi.fn().mockResolvedValue(undefined);
    render(<SessionPauseRequestPanel pauseRequest={permissionPrompt()} onRespond={onRespond} />);
    fireEvent.click(screen.getByText("Allow"));
    await waitFor(() => expect(onRespond).toHaveBeenCalled());
    expect(onRespond.mock.calls[0][0]).toMatchObject({ decision: "answer" });
  });

  it("Deny submits decision=cancel", async () => {
    const onRespond = vi.fn().mockResolvedValue(undefined);
    render(<SessionPauseRequestPanel pauseRequest={permissionPrompt()} onRespond={onRespond} />);
    fireEvent.click(screen.getByText("Deny"));
    await waitFor(() => expect(onRespond).toHaveBeenCalled());
    expect(onRespond.mock.calls[0][0]).toMatchObject({ decision: "cancel" });
  });

  it("structured_question still reads Send answer / Cancel", () => {
    const sq = permissionPrompt({
      kind: "structured_question",
      title: "Which approach?",
      questions: [
        { id: "q1", header: null, question: "Pick one", multi_select: false, options: [{ label: "A", value: "A", description: null }] },
      ],
    } as Partial<SessionPauseRequest>);
    render(<SessionPauseRequestPanel pauseRequest={sq} onRespond={vi.fn()} />);
    expect(screen.getByText("Needs answer")).toBeInTheDocument();
    expect(screen.getByText("Send answer")).toBeInTheDocument();
  });

  it("plan_approval renders approval language without a free-text box", () => {
    const plan = permissionPrompt({
      kind: "plan_approval",
      title: "Plan approval required",
      questions: [
        {
          id: "approval",
          header: null,
          question: "1. Inspect. 2. Patch. 3. Test.",
          multi_select: false,
          options: [
            { label: "Approve", value: "approve", description: null },
            { label: "Reject", value: "reject", description: null },
          ],
        },
      ],
    } as Partial<SessionPauseRequest>);

    render(<SessionPauseRequestPanel pauseRequest={plan} onRespond={vi.fn()} />);

    expect(screen.getByText("Plan approval")).toBeInTheDocument();
    expect(screen.getByText("Approve")).toBeInTheDocument();
    expect(screen.getByText("Reject")).toBeInTheDocument();
    expect(screen.getByText("1. Inspect. 2. Patch. 3. Test.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Answer")).not.toBeInTheDocument();
  });
});
