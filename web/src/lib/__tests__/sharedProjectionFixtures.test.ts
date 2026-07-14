import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { buildTimelineModel } from "../sessionWorkspace";
import type { AgentSessionProjectionItem } from "../../services/api/agents";
import type { TimelineItem } from "../sessionWorkspace";

type ExpectedRow =
  | {
      kind: "action";
      action_kind: string;
      provider: string | null;
      event_id: number | null;
    }
  | {
      kind: "message";
      role: string;
      event_id: number;
    }
  | {
      kind: "tool";
      tool_name: string;
      call_event_id: number;
      result_event_id: number | null;
      pairing: "id" | "fifo" | "pending";
    }
  | {
      kind: "orphan_tool";
      tool_name: string;
      result_event_id: number;
    }
  | {
      kind: "noise_group";
      tool_names: string[];
      call_event_ids: number[];
      result_event_ids: Array<number | null>;
      pairings: Array<"id" | "fifo" | "pending">;
    };

type SharedProjectionFixture = {
  name: string;
  projection: {
    items: AgentSessionProjectionItem[];
  };
  expectations: {
    rows: ExpectedRow[];
    tool_count: number;
    noise_group_count: number;
    orphan_tool_ids: number[];
  };
};

function loadFixture(name: string): SharedProjectionFixture {
  const fixturePath = resolve(process.cwd(), "../tests/fixtures/session-projection", name);
  return JSON.parse(readFileSync(fixturePath, "utf8")) as SharedProjectionFixture;
}

function summarizeRows(items: TimelineItem[]): ExpectedRow[] {
  return items.map((item) => {
    if (item.kind === "message") {
      return {
        kind: "message",
        role: item.event.role,
        event_id: item.event.id,
      };
    }
    if (item.kind === "action") {
      return {
        kind: "action",
        action_kind: item.action.action.kind,
        provider: item.action.action.provider ?? null,
        event_id: item.action.action.event_id ?? null,
      };
    }
    if (item.kind === "noise_group") {
      return {
        kind: "noise_group",
        tool_names: item.group.interactions.map((interaction) => interaction.toolName),
        call_event_ids: item.group.interactions.map((interaction) => interaction.callEvent?.id ?? -1),
        result_event_ids: item.group.interactions.map((interaction) => interaction.resultEvent?.id ?? null),
        pairings: item.group.interactions.map((interaction) => {
          if (interaction.pairing === "orphan") {
            throw new Error("Noise groups cannot contain orphan tool interactions");
          }
          return interaction.pairing;
        }),
      };
    }
    const { interaction } = item;
    if (interaction.pairing === "orphan") {
      if (!interaction.resultEvent) {
        throw new Error("Orphan tool interaction is missing its result event");
      }
      return {
        kind: "orphan_tool",
        tool_name: interaction.toolName,
        result_event_id: interaction.resultEvent.id,
      };
    }
    return {
      kind: "tool",
      tool_name: interaction.toolName,
      call_event_id: interaction.callEvent?.id ?? -1,
      result_event_id: interaction.resultEvent?.id ?? null,
      pairing: interaction.pairing,
    };
  });
}

describe("shared session projection fixtures", () => {
  it.each([
    "tool-pairing-fifo.json",
    "context-boundary-noise-collapse.json",
    "session-action-interrupt.json",
    "exploration-run-web-breaks.json",
    "parallel-tool-id-pairing.json",
  ])(
    "matches %s",
    (fixtureName) => {
      const fixture = loadFixture(fixtureName);
      const model = buildTimelineModel(fixture.projection.items);

      expect(summarizeRows(model.items)).toEqual(fixture.expectations.rows);
      expect(model.toolItems).toHaveLength(fixture.expectations.tool_count);
      expect(model.noiseGroups).toHaveLength(fixture.expectations.noise_group_count);
      expect(
        model.toolItems
          .filter((interaction) => interaction.pairing === "orphan")
          .map((interaction) => interaction.resultEvent?.id),
      ).toEqual(fixture.expectations.orphan_tool_ids);
    },
  );
});
