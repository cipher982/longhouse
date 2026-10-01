import { describe, expect, it } from "vitest";
import type { AgentSession } from "@/shared/api/agents";
import { getDriftTitle, getSessionCardText } from "../sessionLabels";

// Minimal AgentSession stub — getSessionCardText only reads title-related fields.
function makeSession(overrides: Partial<AgentSession> = {}): AgentSession {
  return {
    provider: "codex",
    project: "zerg",
    summary_title: null,
    timeline_title: null,
    first_user_message: null,
    ...overrides,
  } as AgentSession;
}

describe("getSessionCardText", () => {
  it("prefers the server-resolved timeline_title (the frozen anchor)", () => {
    const text = getSessionCardText(
      makeSession({
        timeline_title: "Fix Refresh Token Rotation",
        summary_title: "Now Doing Something Else",
        first_user_message: '"""\nplease help',
      }),
    );
    expect(text.title).toBe("Fix Refresh Token Rotation");
    expect(text.titleSource).toBe("generated");
  });

  it("does not move the headline when summary_title drifts", () => {
    const anchor = "Refresh Token Rotation";
    const a = getSessionCardText(makeSession({ timeline_title: anchor, summary_title: "Topic A" }));
    const b = getSessionCardText(makeSession({ timeline_title: anchor, summary_title: "Topic B" }));
    expect(a.title).toBe(anchor);
    expect(b.title).toBe(anchor);
  });

  it("falls back to summary_title when no timeline_title (pre-anchor payloads)", () => {
    const text = getSessionCardText(makeSession({ summary_title: "Debug Bedrock Race" }));
    expect(text.title).toBe("Debug Bedrock Race");
  });

  it("falls back to the first user message, then a structured label", () => {
    expect(getSessionCardText(makeSession({ first_user_message: "add an endpoint" })).title).toBe(
      "add an endpoint",
    );
    expect(getSessionCardText(makeSession({ project: "zerg", provider: "codex" })).title).toBe(
      "New Codex session in zerg",
    );
  });

  it("shows the served headline once when the server says it is the prompt cut short", () => {
    // No title model: the server's headline is the first six words plus an ellipsis.
    const prompt = "Add rate limiting to the public API and cover it with tests";
    const text = getSessionCardText(
      makeSession({
        timeline_title: "Add rate limiting to the public\u2026",
        title_source: "prompt",
        first_user_message: prompt,
      }),
    );
    // The served headline, verbatim (sanitised and cut by the server), once.
    expect(text.title).toBe("Add rate limiting to the public\u2026");
    expect(text.titleSource).toBe("prompt");
    expect(text.subheading).toBeNull();
  });

  it("keeps the prompt as the subheading under a generated title, even one that opens with the prompt's words", () => {
    const text = getSessionCardText(
      makeSession({
        timeline_title: "Fix login bug",
        title_source: "ai",
        first_user_message: "Fix login bug in the auth module and add a regression test",
      }),
    );
    expect(text.title).toBe("Fix login bug");
    expect(text.subheading).toBe("Fix login bug in the auth module and add a regression test");
  });

  it("renders the server-owned empty-session projection verbatim", () => {
    const text = getSessionCardText(makeSession({ timeline_title: "zerg · Empty session" }));
    expect(text.title).toBe("zerg · Empty session");
  });
});

describe("getDriftTitle", () => {
  it("returns the drifting summary title when it differs from the headline", () => {
    expect(getDriftTitle({ summary_title: "Now wiring retries" }, "Refresh Token Rotation")).toBe(
      "Now wiring retries",
    );
  });

  it("suppresses the drift line when it would echo the headline", () => {
    expect(getDriftTitle({ summary_title: "Refresh Token Rotation" }, "Refresh Token Rotation")).toBeNull();
  });

  it("returns null when there is no summary title", () => {
    expect(getDriftTitle({ summary_title: null }, "Anything")).toBeNull();
  });

  it("suppresses the drift line when the headline was truncated to the same text", () => {
    // Headline truncated with an ellipsis; drift equals the full prefix -> echo.
    expect(
      getDriftTitle({ summary_title: "Refresh token rotation hardening pass" }, "Refresh token rotation…"),
    ).toBeNull();
  });
});
