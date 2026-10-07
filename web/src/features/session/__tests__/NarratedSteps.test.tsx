import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TimelinePane } from "../TimelinePane";
import { declaredTimeoutSeconds, type TimelineLiveness } from "../RunningCallRow";
import { getToolIntentLabel, type TimelineItem, type ToolInteraction } from "@/shared/session/model";
import type { AgentEvent } from "@/shared/api/agents";
import { makeSessionStateFacts } from "@/shared/test/sessionState";

function event(id: number, overrides: Partial<AgentEvent>): AgentEvent {
  return {
    id,
    role: "assistant",
    content_text: null,
    tool_name: null,
    tool_input_json: null,
    tool_output_text: null,
    tool_call_id: null,
    timestamp: "2026-04-15T16:10:00Z",
    in_active_context: true,
    ...overrides,
  };
}

function thought(id: number, timestamp: string, text: string): TimelineItem {
  return {
    kind: "reasoning",
    event: event(id, { role: "system", interaction_kind: "provider_reasoning", content_text: `Thinking:\n${text}`, timestamp }),
  };
}

function bash(
  id: number,
  input: Record<string, unknown>,
  output: string | null,
  timestamp = "2026-04-15T16:10:05Z",
): TimelineItem {
  const callEvent = event(id, {
    tool_name: "Bash",
    tool_input_json: input,
    tool_call_id: `tc-${id}`,
    timestamp,
    tool_call_state: output == null ? "running" : "completed",
  });
  const resultEvent = output == null
    ? null
    : event(id + 1, { role: "tool", tool_name: "Bash", tool_output_text: output, tool_call_id: `tc-${id}`, timestamp });
  const interaction: ToolInteraction = {
    key: `id:tc-${id}`,
    toolName: "Bash",
    callEvent,
    resultEvent,
    pairing: resultEvent ? "id" : "pending",
    anchorId: id,
    timestamp,
    presentation: null,
  };
  return { kind: "tool", interaction };
}

function renderPane(items: TimelineItem[], liveness: TimelineLiveness | null = null, provider: string | null = "claude") {
  return render(
    <TimelinePane
      items={items}
      totalEntries={items.length}
      loadedEntries={items.length}
      abandonedEvents={0}
      showAbandonedBranches={false}
      onShowAbandonedBranchesChange={vi.fn()}
      hasPreviousPage={false}
      isFetchingPreviousPage={false}
      onFetchPreviousPage={vi.fn()}
      loading={false}
      error={null}
      selectedKey={null}
      onSelectKey={vi.fn()}
      provider={provider}
      liveness={liveness}
    />,
  );
}

const longOutput = Array.from({ length: 20 }, (_, index) => `output line ${index}`).join("\n");

describe("narrated steps", () => {
  it("trails the calls after a thought under it and keeps their output closed", () => {
    renderPane([
      thought(1, "2026-04-15T16:10:00Z", "Reading the registry first."),
      bash(2, { command: "cat registry.yml", description: "Reading the registry" }, longOutput),
    ]);

    const row = screen.getByTestId("session-timeline-row");
    expect(row).toHaveClass("tl-trail");
    expect(row).toHaveTextContent("Reading the registry");
    expect(screen.queryByTestId("tool-output-preview")).not.toBeInTheDocument();
  });

  it("keeps a failed trailing call's failure preview visible", () => {
    renderPane([
      thought(1, "2026-04-15T16:10:00Z", "Trying the raw endpoint."),
      bash(
        2,
        { command: "make deploy" },
        ["Wall time: 1.2 seconds", "Process exited with code 2", "Output:", "make: *** [deploy] Error 2"].join("\n"),
      ),
    ]);
    expect(screen.getByTestId("tool-failure-preview")).toBeInTheDocument();
  });

  it("leaves a call without a thought before it on its existing path", () => {
    renderPane([bash(2, { command: "make build" }, longOutput)]);
    expect(screen.getByTestId("session-timeline-row")).not.toHaveClass("tl-trail");
    expect(screen.getByTestId("tool-output-preview")).toBeInTheDocument();
  });

  it("shows a thought's clock only when the minute changed", () => {
    renderPane([
      thought(1, "2026-04-15T16:10:00Z", "First."),
      thought(2, "2026-04-15T16:10:40Z", "Same minute."),
      thought(3, "2026-04-15T16:11:05Z", "Next minute."),
    ]);
    const rows = screen.getAllByTestId("session-timeline-reasoning");
    expect(rows[0].querySelector(".tl-thought__time")).not.toBeNull();
    expect(rows[1].querySelector(".tl-thought__time")).toBeNull();
    expect(rows[2].querySelector(".tl-thought__time")).not.toBeNull();
    expect(rows[0]).not.toHaveTextContent("Thinking");
  });
});

describe("a running call", () => {
  const now = Date.parse("2026-04-15T16:12:00Z");
  afterEach(() => {
    vi.useRealTimers();
  });

  function liveness(validUntil: string, observedAt: string): TimelineLiveness {
    return {
      facts: makeSessionStateFacts({ activity: "executing", observedAt, activityValidUntil: validUntil, access: "live_control" }),
      host: "cinder",
    };
  }

  const longCall = () =>
    bash(
      10,
      { command: "uv run python scripts/basis_search_l0.py --procs 6", description: "Run Stage A basis search", timeout: 3_600_000 },
      null,
      "2026-04-15T15:30:07Z",
    );

  it("leads with the description, keeps the command beneath, and runs its clock on a fresh claim", () => {
    vi.useFakeTimers();
    vi.setSystemTime(now);
    renderPane([longCall()], liveness("2026-04-15T16:20:00Z", "2026-04-15T16:11:56Z"));

    const row = screen.getByTestId("session-timeline-row");
    expect(row).toHaveAttribute("data-run-state", "live");
    expect(row).toHaveTextContent("Run Stage A basis search");
    expect(row).toHaveTextContent("basis_search_l0.py --procs 6");
    expect(screen.getByTestId("running-call-clock")).toHaveTextContent("41:53");
    expect(screen.getByTestId("running-call-clock")).toHaveTextContent("of 1:00:00 timeout");
    expect(row.querySelector(".tl-run__shimmer")).not.toBeNull();
    expect(screen.getByTestId("running-call-note")).toHaveTextContent("Last signal from cinder 4 s ago");
  });

  it("goes still and says it cannot confirm once the claim lapses", () => {
    vi.useFakeTimers();
    vi.setSystemTime(now);
    renderPane([longCall()], liveness("2026-04-15T16:10:00Z", "2026-04-15T16:09:00Z"));

    const row = screen.getByTestId("session-timeline-row");
    expect(row).toHaveAttribute("data-run-state", "uncertain");
    expect(row.querySelector(".tl-run__shimmer")).toBeNull();
    // The clock stops at the last signal rather than running on inference.
    expect(screen.getByTestId("running-call-clock")).toHaveTextContent("38:53");
    expect(screen.getByTestId("running-call-note")).toHaveTextContent(
      "Activity uncertain: no signal from cinder for 3 min",
    );
  });

  it("does not animate a call the model has already moved past", () => {
    vi.useFakeTimers();
    vi.setSystemTime(now);
    renderPane(
      [longCall(), thought(20, "2026-04-15T16:11:00Z", "Meanwhile, the docs.")],
      liveness("2026-04-15T16:20:00Z", "2026-04-15T16:11:56Z"),
    );
    expect(screen.getByTestId("session-timeline-row")).toHaveAttribute("data-run-state", "quiet");
  });

  it("keeps the plain row when the page has no session evidence", () => {
    renderPane([longCall()], null);
    expect(screen.getByTestId("session-timeline-row")).not.toHaveAttribute("data-run-state");
  });
});

describe("declared timeouts", () => {
  const call = (input: Record<string, unknown>) => (bash(1, input, null) as Extract<TimelineItem, { kind: "tool" }>).interaction;

  it("reads units only where the provider contract fixes them", () => {
    expect(declaredTimeoutSeconds(call({ timeout: 600_000 }), "claude")).toBe(600);
    expect(declaredTimeoutSeconds(call({ timeout: 240 }), "omp")).toBe(240);
    expect(declaredTimeoutSeconds(call({ timeout_ms: 30_000 }), "codex")).toBe(30);
    expect(declaredTimeoutSeconds(call({ timeout: 240 }), "opencode")).toBeNull();
    expect(declaredTimeoutSeconds(call({ command: "ls" }), "claude")).toBeNull();
  });
});

describe("tool intent", () => {
  it("reads Claude's shell description as intent, but not another tool's description", () => {
    const shell = (bash(1, { command: "make test", description: "Run the tests" }, "ok") as Extract<TimelineItem, { kind: "tool" }>).interaction;
    expect(getToolIntentLabel(shell)).toBe("Run the tests");
    const other: ToolInteraction = { ...shell, toolName: "WebFetch", callEvent: { ...shell.callEvent!, tool_name: "WebFetch" } };
    expect(getToolIntentLabel(other)).toBeNull();
  });
});
