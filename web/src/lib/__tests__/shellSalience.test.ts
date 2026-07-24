/**
 * Shell salience classifier — shared conformance corpus + grouping wiring.
 *
 * The fixture file is the parity contract with iOS (ShellSalienceTests.swift
 * runs the same corpus). A false demotion here is a safety failure of the
 * whole Change B design, not a cosmetic bug.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { classifyShellCommand, isShellTool } from "../sessionWorkspace/shellSalience";
import { buildTimelineModel } from "../sessionWorkspace";
import type { AgentEvent, AgentSessionProjectionItem } from "../../services/api/agents";

interface FixtureCase {
  command: string;
  expect: "read" | "opaque";
  aggregate?: "search" | "read" | "list";
}

const fixturesPath = resolve(
  fileURLToPath(import.meta.url),
  "../../../../../config/shell-salience-fixtures.json",
);
const fixtures = JSON.parse(readFileSync(fixturesPath, "utf-8")) as { cases: FixtureCase[] };

describe("classifyShellCommand conformance corpus", () => {
  it("has both read and adversarial opaque coverage", () => {
    const reads = fixtures.cases.filter((c) => c.expect === "read");
    const opaques = fixtures.cases.filter((c) => c.expect === "opaque");
    expect(reads.length).toBeGreaterThan(20);
    expect(opaques.length).toBeGreaterThan(40);
  });

  for (const fixture of fixtures.cases) {
    it(`${fixture.expect}: ${JSON.stringify(fixture.command).slice(0, 80)}`, () => {
      const result = classifyShellCommand(fixture.command);
      if (fixture.expect === "opaque") {
        expect(result).toBeNull();
      } else {
        expect(result).not.toBeNull();
        expect(result?.tier).toBe("noise");
        expect(result?.aggregate).toBe(fixture.aggregate);
      }
    });
  }
});

describe("isShellTool", () => {
  it("covers the configured shell tools and nothing surprising", () => {
    for (const name of ["Bash", "shell", "shell_command", "exec_command", "run_shell_command"]) {
      expect(isShellTool(name)).toBe(true);
    }
    expect(isShellTool("write_stdin")).toBe(false);
    expect(isShellTool("Read")).toBe(false);
  });
});

function toolPair(
  id: number,
  toolName: string,
  command: string,
  timestamp: string,
  output = "ok",
): AgentEvent[] {
  return [
    {
      id,
      role: "assistant",
      timestamp,
      content_text: null,
      tool_name: toolName,
      tool_input_json: { command } as never,
      tool_output_text: null,
      tool_call_id: `call-${id}`,
      in_active_context: true,
      is_head_branch: true,
    } as AgentEvent,
    {
      id: id + 1,
      role: "tool",
      timestamp,
      content_text: null,
      tool_name: toolName,
      tool_input_json: null,
      tool_output_text: output,
      tool_call_id: `call-${id}`,
      in_active_context: true,
      is_head_branch: true,
    } as AgentEvent,
  ];
}

function projection(events: AgentEvent[]): AgentSessionProjectionItem[] {
  return events.map((evt) => ({
    kind: "event" as const,
    session_id: "s1",
    timestamp: evt.timestamp,
    event: evt,
  }));
}

describe("timeline grouping with shell salience", () => {
  const t = "2026-01-01T00:00:00Z";

  it("collapses consecutive read-only Bash commands into one exploration run", () => {
    const events = [
      ...toolPair(1, "Bash", "grep -rn pattern web/src", t),
      ...toolPair(3, "Bash", "ls -la web/src/lib", t),
      ...toolPair(5, "Bash", "cat package.json", t),
    ];
    const model = buildTimelineModel(projection(events));
    const groups = model.items.filter((item) => item.kind === "activity_group");
    expect(groups).toHaveLength(1);
    expect(groups[0].kind === "activity_group" && groups[0].group.interactions).toHaveLength(3);
  });

  it("groups completed shell work while retaining singleton salience metadata", () => {
    const events = [
      ...toolPair(1, "Bash", "grep -rn pattern web/src", t),
      ...toolPair(3, "Bash", "rm -rf node_modules", t),
      ...toolPair(5, "Bash", "ls -la web/src/lib", t),
    ];
    const model = buildTimelineModel(projection(events));
    const groups = model.items.filter((item) => item.kind === "activity_group");
    expect(groups).toHaveLength(1);
    expect(groups[0].kind === "activity_group" && groups[0].group.interactions).toHaveLength(3);
  });

  it("read-only Bash with a nonzero exit stays out of exploration runs", () => {
    const failedOutput = [
      "Chunk ID: fixture",
      "Wall time: 0.1 seconds",
      "Process exited with code 1",
      "Original token count: 5",
      "Output:",
      "grep: no matches",
    ].join("\n");
    const events = [
      ...toolPair(1, "Bash", "grep -rn missing web/src", t, failedOutput),
      ...toolPair(3, "Bash", "ls -la web/src/lib", t),
      ...toolPair(5, "Bash", "cat package.json", t),
    ];
    const model = buildTimelineModel(projection(events));
    const groups = model.items.filter((item) => item.kind === "activity_group");
    expect(groups).toHaveLength(1);
    expect(groups[0].kind === "activity_group" && groups[0].group.interactions).toHaveLength(2);
  });

  it("native noise tools and shell reads join the same run", () => {
    const grepPair: AgentEvent[] = [
      {
        id: 1,
        role: "assistant",
        timestamp: t,
        content_text: null,
        tool_name: "Grep",
        tool_input_json: { pattern: "x" } as never,
        tool_output_text: null,
        tool_call_id: "call-1",
        in_active_context: true,
        is_head_branch: true,
      } as AgentEvent,
      {
        id: 2,
        role: "tool",
        timestamp: t,
        content_text: null,
        tool_name: "Grep",
        tool_input_json: null,
        tool_output_text: "ok",
        tool_call_id: "call-1",
        in_active_context: true,
        is_head_branch: true,
      } as AgentEvent,
    ];
    const events = [...grepPair, ...toolPair(3, "Bash", "ls -la web", t)];
    const model = buildTimelineModel(projection(events));
    const groups = model.items.filter((item) => item.kind === "activity_group");
    expect(groups).toHaveLength(1);
    expect(groups[0].kind === "activity_group" && groups[0].group.interactions).toHaveLength(2);
  });
});
