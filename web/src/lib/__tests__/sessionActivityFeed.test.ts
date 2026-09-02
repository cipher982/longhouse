import { describe, expect, it, vi } from "vitest";
import {
  SessionActivityFeed,
  classifyWorkspaceChange,
} from "../sessionActivityFeed";
import type { SessionTranscriptPreview } from "../../services/api/agents";

function preview(overrides: Partial<SessionTranscriptPreview>): SessionTranscriptPreview {
  return {
    event_id: 1,
    text: "hello",
    tool_name: null,
    event_origin: "durable",
    is_provisional: false,
    is_complete: true,
    is_stale: false,
    ...overrides,
  };
}

describe("classifyWorkspaceChange", () => {
  it("treats a frame without a preview as a runtime-state wake", () => {
    expect(classifyWorkspaceChange({ transcript_preview: null })).toBe("state");
    expect(classifyWorkspaceChange({})).toBe("state");
  });

  it("splits tool frames by call state", () => {
    expect(
      classifyWorkspaceChange({
        transcript_preview: preview({ tool_name: "Bash", tool_call_state: "running" }),
      }),
    ).toBe("tool_start");
    expect(
      classifyWorkspaceChange({
        transcript_preview: preview({ tool_name: "Bash", tool_call_state: "completed" }),
      }),
    ).toBe("tool_result");
  });

  it("reads provisional assistant text as a delta and durable text as a message", () => {
    expect(
      classifyWorkspaceChange({
        transcript_preview: preview({ is_provisional: true, event_origin: "live_provisional" }),
      }),
    ).toBe("text_delta");
    expect(classifyWorkspaceChange({ transcript_preview: preview({}) })).toBe("message");
  });
});

describe("SessionActivityFeed", () => {
  it("records frames in order and notifies subscribers", () => {
    let now = 1000;
    const feed = new SessionActivityFeed(() => now);
    const listener = vi.fn();
    const unsubscribe = feed.subscribe(listener);

    feed.push("tool_start");
    now = 1250;
    feed.push("text_delta");

    expect(feed.snapshot()).toEqual([
      { at: 1000, kind: "tool_start" },
      { at: 1250, kind: "text_delta" },
    ]);
    expect(listener).toHaveBeenCalledTimes(2);
    expect(listener).toHaveBeenLastCalledWith({ at: 1250, kind: "text_delta" });

    unsubscribe();
    feed.push("message");
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("caps retained frames and clears on reset", () => {
    const feed = new SessionActivityFeed(() => 0);
    for (let index = 0; index < 450; index += 1) {
      feed.push("text_delta", index);
    }
    expect(feed.snapshot()).toHaveLength(400);
    expect(feed.snapshot()[0]).toEqual({ at: 50, kind: "text_delta" });

    feed.reset();
    expect(feed.snapshot()).toHaveLength(0);
  });
});
