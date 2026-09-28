import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { TimelineSessionCard } from "@/shared/api/agents";
import { makeSessionStateFacts } from "@/shared/test/sessionState";
import { SessionRow } from "../SessionRow";

function card(sessionState: ReturnType<typeof makeSessionStateFacts>): TimelineSessionCard {
  return {
    thread_id: "thread-1",
    head: {
      id: "sess-1",
      provider: "claude",
      project: "zerg",
      device_id: "cinder",
      git_branch: "main",
      started_at: "2026-05-19T15:00:00Z",
      last_activity_at: "2026-05-19T15:59:00Z",
      timeline_title: "Port the fire into timeline rows",
      session_state: sessionState,
    },
  } as unknown as TimelineSessionCard;
}

function renderRow(thread: TimelineSessionCard) {
  const queryClient = new QueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionRow thread={thread} onClick={() => {}} relativeNowMs={Date.parse("2026-05-19T16:00:00Z")} />
    </QueryClientProvider>,
  );
}

describe("SessionRow status label", () => {
  it("shows the server's primary label verbatim", () => {
    const facts = makeSessionStateFacts({ activity: "thinking", tool: "Bash", observedAt: "2026-05-19T15:59:00Z" });
    renderRow(card(facts));
    const activity = screen.getByTestId("session-row").querySelector(".inbox-row-activity");
    expect(activity).toHaveTextContent("Thinking");
    expect(activity).not.toHaveTextContent("Bash");
    expect(activity).not.toHaveTextContent("Working");
  });

  it("shows the server's words for a tool it is running", () => {
    const facts = makeSessionStateFacts({ activity: "executing", observedAt: "2026-05-19T15:59:00Z" });
    renderRow(card(facts));
    expect(screen.getByTestId("session-row").querySelector(".inbox-row-activity")).toHaveTextContent("Using Shell");
  });

  it("demotes an expired work claim instead of repeating the served label", () => {
    const facts = makeSessionStateFacts({
      activity: "executing",
      observedAt: "2026-05-19T15:40:00Z",
      activityValidUntil: "2026-05-19T15:45:00Z",
    });
    renderRow(card(facts));
    const row = screen.getByTestId("session-row");
    const activity = row.querySelector(".inbox-row-activity");
    expect(activity).toHaveTextContent("Activity uncertain");
    expect(activity).not.toHaveTextContent("Using Shell");
    expect(row).toHaveAttribute("data-status", "unknown");
    expect(activity).toHaveAttribute("data-signal", "unknown");
  });

  it("keeps the served label while the claim is still fresh", () => {
    const facts = makeSessionStateFacts({
      activity: "executing",
      observedAt: "2026-05-19T15:59:00Z",
      activityValidUntil: "2026-05-19T16:05:00Z",
    });
    renderRow(card(facts));
    expect(screen.getByTestId("session-row").querySelector(".inbox-row-activity")).toHaveTextContent("Using Shell");
  });
});
