import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import type { AgentSessionProjectionResponse } from "../../../../services/api/agents";
import { getInteractionDisplayInfo, getToolSummary } from "../../../../lib/sessionWorkspace";
import { buildLiveTimelineModel, flattenLiveItems, toolResultLine } from "../liveProjection";

/**
 * The landing phone must consume the same canonical projection the web/iOS
 * timeline does. If a second, landing-specific pairing/presentation path is
 * re-introduced, this test (and the shared fixture test it piggybacks on)
 * fails before the phone can drift from the real timeline.
 */
function loadProjection(): AgentSessionProjectionResponse {
  const fixturePath = resolve(
    process.cwd(),
    "../tests/fixtures/session-projection/live-claude-session.json",
  );
  const fixture = JSON.parse(readFileSync(fixturePath, "utf8")) as {
    projection: AgentSessionProjectionResponse;
  };
  return fixture.projection;
}

describe("live phone projection", () => {
  it("derives phone rows from the canonical timeline model, not a parallel projector", () => {
    const projection = loadProjection();
    const model = buildLiveTimelineModel(projection);
    const items = flattenLiveItems(model.items);

    // user message + Read/Edit/Bash tool rows + final answer.
    const toolRows = items.filter((item) => item.kind === "tool");
    const prose = items.filter(
      (item) => item.kind === "message" && item.event.role === "assistant",
    );

    expect(toolRows.map((row) => getInteractionDisplayInfo(row.interaction).displayName)).toEqual([
      "Read",
      "Edit",
      "Bash",
    ]);
    expect(toolRows.map((row) => getToolSummary(row.interaction))).toEqual([
      "/demo-repo/inventory.py",
      "/demo-repo/inventory.py",
      "python3 test_inventory.py",
    ]);
    expect(toolRows.map((row) => toolResultLine(row.interaction))).toEqual([
      "def count_items...",
      "Updated inventory.py",
      "all tests passed",
    ]);
    expect(prose.map((row) => row.event.content_text)).toEqual(["Fixed it. All tests pass."]);
  });

  it("keeps the user instruction out of the assistant transcript", () => {
    const model = buildLiveTimelineModel(loadProjection());
    const items = flattenLiveItems(model.items);
    const userProse = items.filter(
      (item) => item.kind === "message" && item.event.role === "user",
    );
    // The submitted instruction is rendered once by the composer path, so the
    // phone must not double-render it from the projection.
    expect(userProse.map((row) => row.event.content_text)).toEqual(["Fix the off-by-one bug"]);
  });

  it("toolResultLine strips wrapper metadata and returns the last non-empty line", () => {
    const projection = loadProjection();
    // The Bash result carries the raw Longhouse runner shape.
    const bash = projection.items.find((item) => item.event.id === 7)!;
    bash.event.tool_output_text =
      "Chunk ID: abc\nWall time: 0.12 seconds\nProcess exited with code 0\nOutput:\nall tests passed\n";
    const model = buildLiveTimelineModel(projection);
    const bashRow = flattenLiveItems(model.items).find(
      (item) => item.kind === "tool" && item.interaction.toolName === "Bash",
    );
    expect(bashRow?.kind === "tool" ? toolResultLine(bashRow.interaction) : null).toBe("all tests passed");
  });
});
