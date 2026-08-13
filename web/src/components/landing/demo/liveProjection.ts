import type { AgentSessionProjectionResponse } from "../../../services/api/agents";
import {
  buildTimelineModel,
  parseLonghouseOutput,
} from "../../../lib/sessionWorkspace";
import type { TimelineItem, TimelineModel, ToolInteraction } from "../../../lib/sessionWorkspace";

/**
 * The landing phone derives its transcript from the same canonical projection
 * the web/iOS timeline uses — `buildTimelineModel`. No landing-specific pairing
 * or presentation remains: pairing (id/fifo/orphan), activity grouping, and
 * tool presentation (generated tool-tiers + shell-salience fallbacks) all come
 * from the shared sessionWorkspace module that is parity-tested against iOS
 * via `tests/fixtures/session-projection/*`.
 */
export function buildLiveTimelineModel(
  response: AgentSessionProjectionResponse,
): TimelineModel {
  return buildTimelineModel(response.items);
}

/**
 * Flatten the canonical timeline for the compact phone surface. The web/iOS
 * timeline collapses runs of 2+ completed exploration calls into one
 * `activity_group` chip; the demo phone renders each call as its own row so
 * the visitor watches Claude's real tool calls stream in. This is a UI choice,
 * not a second projection — the grouping still lives in the model.
 */
export function flattenLiveItems(items: TimelineItem[]): TimelineItem[] {
  return items.flatMap((item) =>
    item.kind === "activity_group"
      ? item.group.interactions.map((interaction): TimelineItem => ({ kind: "tool", interaction }))
      : [item],
  );
}

/**
 * One-line "result" the phone shows under a tool row: the last non-empty line
 * of the real output, with the Longhouse runner wrapper metadata stripped.
 */
export function toolResultLine(interaction: ToolInteraction): string | undefined {
  const raw = interaction.resultEvent?.tool_output_text;
  if (!raw) return undefined;
  const parsed = parseLonghouseOutput(raw);
  const text = parsed ? parsed.output : raw;
  const lines = text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  return lines.at(-1);
}
