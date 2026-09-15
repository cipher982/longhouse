import { describe, expect, it } from "vitest";
import {
  buildSessionMetaSentence,
  buildSessionMetaSentenceParts,
  formatElapsedClock,
  getSessionHeaderState,
} from "../sessionHeaderState";
import type { AgentSession } from "../../../services/api/agents";

function session(overrides: {
  disposition?: string;
  pendingInteraction?: unknown;
  activityState?: string;
  tool?: string | null;
  observedAt?: string | null;
  primaryTone?: string | null;
  primaryLabel?: string | null;
  lastResultAt?: string | null;
}): Pick<AgentSession, "session_state"> {
  return {
    session_state: {
      disposition: { state: overrides.disposition ?? "open" },
      pending_interaction: overrides.pendingInteraction ?? null,
      activity: {
        state: overrides.activityState ?? "quiescent",
        tool: overrides.tool ?? null,
        observed_at: overrides.observedAt ?? null,
      },
      presentation: {
        primary:
          overrides.primaryTone != null
            ? { tone: overrides.primaryTone, label: overrides.primaryLabel ?? "" }
            : null,
      },
      last_result_at: overrides.lastResultAt ?? null,
    } as never,
  };
}

describe("getSessionHeaderState", () => {
  it("reads a real provider question as attention, not idle", () => {
    const state = getSessionHeaderState(
      session({ pendingInteraction: { id: "1" } }),
      Date.now(),
    );
    expect(state).toEqual({ tone: "attention", text: "Waiting for approval" });
  });

  it("reads a blocked/stalled presentation tone as attention even when activity.state is quiescent", () => {
    const state = getSessionHeaderState(
      session({ primaryTone: "stalled", primaryLabel: "No progress for 31m" }),
      Date.now(),
    );
    expect(state).toEqual({ tone: "attention", text: "No progress for 31m" });
  });

  it("reads an executing/thinking session as live, with a tool-named sentence", () => {
    const now = Date.parse("2026-04-15T16:30:00Z");
    const state = getSessionHeaderState(
      session({
        activityState: "executing",
        tool: "hub",
        observedAt: "2026-04-15T15:55:00Z",
        primaryTone: "running",
      }),
      now,
    );
    expect(state.tone).toBe("live");
    expect(state.text).toBe("Using hub for 35 minutes");
  });

  it("reads a closed session as cool/ended", () => {
    const state = getSessionHeaderState(
      session({ disposition: "closed", lastResultAt: "2026-04-15T16:12:00Z" }),
      Date.now(),
    );
    expect(state.tone).toBe("cool");
    expect(state.text).toMatch(/^Ended/);
  });

  it("reads an open, non-working session as idle", () => {
    const state = getSessionHeaderState(
      session({ lastResultAt: "2026-04-15T16:12:00Z" }),
      Date.now(),
    );
    expect(state.tone).toBe("cool");
    expect(state.text).toMatch(/^Idle since/);
  });
});

describe("buildSessionMetaSentence", () => {
  it("builds the full sentence from provider, project, host, and counts", () => {
    expect(
      buildSessionMetaSentence({
        provider: "OMP",
        project: "zerg",
        host: "cinder",
        messages: 57,
        toolCalls: 334,
        tone: "live",
      }),
    ).toBe("OMP working in zerg on cinder, 57 messages and 334 tool calls so far");
  });

  it("drops missing parts gracefully instead of leaving stray punctuation", () => {
    expect(
      buildSessionMetaSentence({
        provider: "OMP",
        project: null,
        host: null,
        messages: 0,
        toolCalls: 0,
        tone: "live",
      }),
    ).toBe("OMP working");
  });

  it("falls back to counts alone when nothing else is known", () => {
    expect(
      buildSessionMetaSentence({
        provider: null,
        project: null,
        host: null,
        messages: 3,
        toolCalls: 0,
        tone: "live",
      }),
    ).toBe("3 messages so far");
  });

  it("returns null when there is nothing to say", () => {
    expect(
      buildSessionMetaSentence({
        provider: null,
        project: null,
        host: null,
        messages: 0,
        toolCalls: 0,
        tone: "live",
      }),
    ).toBeNull();
  });

  it("drops \"working\" when the header tone is not live, so an ended session isn't claimed as still working", () => {
    expect(
      buildSessionMetaSentence({
        provider: "OMP",
        project: "zerg",
        host: "cinder",
        messages: 57,
        toolCalls: 334,
        tone: "cool",
      }),
    ).toBe("OMP in zerg on cinder, 57 messages and 334 tool calls so far");
  });

  it("drops \"working\" for the attention tone too", () => {
    expect(
      buildSessionMetaSentence({
        provider: "OMP",
        project: "zerg",
        host: "cinder",
        messages: 0,
        toolCalls: 0,
        tone: "attention",
      }),
    ).toBe("OMP in zerg on cinder");
  });
});

describe("buildSessionMetaSentenceParts", () => {
  it("splits the sentence around the tool-call count, joining back to the same text", () => {
    const parts = buildSessionMetaSentenceParts({
      provider: "OMP",
      project: "zerg",
      host: "cinder",
      messages: 57,
      toolCalls: 334,
      tone: "live",
    });
    expect(parts).not.toBeNull();
    expect(`${parts!.before}334 ${parts!.toolCallsWord}${parts!.after}`).toBe(
      "OMP working in zerg on cinder, 57 messages and 334 tool calls so far",
    );
  });

  it("returns null when there are no tool calls to highlight", () => {
    expect(
      buildSessionMetaSentenceParts({
        provider: "OMP",
        project: "zerg",
        host: "cinder",
        messages: 57,
        toolCalls: 0,
        tone: "live",
      }),
    ).toBeNull();
  });

  it("drops the messages clause and the leading sentence when neither is known", () => {
    const parts = buildSessionMetaSentenceParts({
      provider: null,
      project: null,
      host: null,
      messages: 0,
      toolCalls: 5,
      tone: "live",
    });
    expect(parts).toEqual({
      before: "",
      toolCalls: 5,
      toolCallsWord: "tool calls",
      after: " so far",
    });
  });

  it("uses the singular word for exactly one tool call", () => {
    const parts = buildSessionMetaSentenceParts({
      provider: null,
      project: null,
      host: null,
      messages: 0,
      toolCalls: 1,
      tone: "live",
    });
    expect(parts?.toolCallsWord).toBe("tool call");
  });

  it("drops \"working\" when the header tone is not live", () => {
    const parts = buildSessionMetaSentenceParts({
      provider: "OMP",
      project: "zerg",
      host: "cinder",
      messages: 57,
      toolCalls: 334,
      tone: "cool",
    });
    expect(parts).not.toBeNull();
    expect(`${parts!.before}334 ${parts!.toolCallsWord}${parts!.after}`).toBe(
      "OMP in zerg on cinder, 57 messages and 334 tool calls so far",
    );
  });
});

describe("formatElapsedClock", () => {
  it("formats minutes:seconds", () => {
    expect(formatElapsedClock(35 * 60 + 37)).toBe("35:37");
  });

  it("formats hours:minutes:seconds past an hour", () => {
    expect(formatElapsedClock(60 * 65 + 5)).toBe("1:05:05");
  });
});
