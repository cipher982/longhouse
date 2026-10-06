import { describe, expect, it } from "vitest";
import { hydrateLiteProjection, hydrateLiteWorkspace } from "../liteTranscript";
import type { AgentSessionProjectionResponse, AgentSessionWorkspaceResponse } from "../agents";

const lite = {
  root_session_id: "s-1",
  focus_session_id: "s-1",
  head_session_id: "s-1",
  path_session_ids: ["s-1"],
  total: 3,
  generation_id: "g-1",
  next_cursor: "older",
  has_more: true,
  detail: "lite",
  tool_presentations: { p1: { version: 2, tool_name: "bash", label: "Bash", wrapper_recedes: false } },
  items: [
    { kind: "event", timestamp: "2026-10-06T12:00:00Z", event: { id: "e1", cursor: "c1", role: "user", content_text: "run the tests" } },
    {
      kind: "event",
      timestamp: "2026-10-06T12:00:01Z",
      event: {
        id: "e2",
        cursor: "c2",
        role: "assistant",
        tool_name: "Bash",
        tool_call_id: "t1",
        tool_input_json: { command: "pytest -q" },
        tool_presentation_ref: "p1",
        tool_presentation_input: "same",
        tool_presentation_shell_summary: { aggregate: null },
      },
    },
    {
      kind: "event",
      timestamp: "2026-10-06T12:00:02Z",
      event: {
        id: "e3",
        cursor: "c3",
        role: "tool",
        tool_call_id: "t1",
        tool_output_text: "line 1\nline 2\n… 30 more lines …",
        tool_output_truncated: true,
        tool_output_original_chars: 9000,
        in_active_context: false,
      },
    },
  ],
} as unknown as AgentSessionProjectionResponse;

describe("hydrateLiteProjection", () => {
  it("rebuilds the full event shape a full page would carry", () => {
    const full = hydrateLiteProjection(lite);
    const [user, call, result] = full.items;

    expect(full).not.toHaveProperty("detail");
    expect(full).not.toHaveProperty("tool_presentations");
    expect(full.next_cursor).toBe("older");
    expect(user.session_id).toBe("s-1");
    expect(user.action).toBeNull();
    expect(user.event).toMatchObject({
      id: "e1",
      content_text: "run the tests",
      tool_name: null,
      tool_output_text: null,
      tool_input_json: null,
      tool_presentation: null,
      timestamp: "2026-10-06T12:00:00Z",
      in_active_context: true,
      is_head_branch: true,
      media_refs: [],
    });
    expect(user.event).not.toHaveProperty("lite_body");
    expect(call.event?.tool_presentation).toMatchObject({
      label: "Bash",
      tool_input_json: { command: "pytest -q" },
      shell_summary: { aggregate: null },
      children: [],
    });
    expect(result.event?.in_active_context).toBe(false);
    expect(result.event?.lite_body).toEqual({ session_id: "s-1", cursor: "c3" });
  });

  it("leaves a full page untouched", () => {
    const full = { ...lite, detail: undefined } as unknown as AgentSessionProjectionResponse;
    expect(hydrateLiteProjection(full)).toBe(full);
  });
});

describe("hydrateLiteWorkspace", () => {
  it("restores the thread's copy of the session", () => {
    const session = { id: "s-1" } as AgentSessionWorkspaceResponse["session"];
    const workspace = {
      session,
      thread: { root_session_id: "s-1", head_session_id: "s-1" },
      projection: lite,
      workspace_revision: { fingerprint: "f" },
    } as unknown as AgentSessionWorkspaceResponse;

    const full = hydrateLiteWorkspace(workspace);

    expect(full.thread.sessions).toEqual([session]);
    expect(full.projection.items[2].event?.lite_body?.cursor).toBe("c3");
  });
});
