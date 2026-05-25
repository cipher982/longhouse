import type { AgentEvent, AgentSession, AgentSessionProjectionItem } from "../../services/api/agents";
import { parseUTC } from "../dateUtils";
import type {
  NoiseGroup,
  TimelineItem,
  TimelineModel,
  TimelineSeam,
  TimelineSelection,
  ToolInteraction,
} from "./types";
import { truncatePath } from "./formatters";
import {
  colorTokenToCss,
  resolveToolInfo,
  toolTier,
  type ResolvedToolInfo,
  type ToolTier,
} from "./toolTiers.generated";

export function isOutsideActiveContext(event: AgentEvent | null | undefined): boolean {
  return event?.in_active_context === false;
}

export function isAgentToolInteraction(interaction: ToolInteraction): boolean {
  return interaction.toolName.toLowerCase() === "agent";
}

export function getToolTier(interaction: ToolInteraction): ToolTier {
  return toolTier(interaction.toolName);
}

/** Back-compat display info with CSS color strings for inline styles. */
export function getToolDisplayInfo(toolName: string): {
  icon: string;
  color: string;
  displayName: string;
  mcpNamespace?: string;
  tier: ToolTier;
} {
  const info: ResolvedToolInfo = resolveToolInfo(toolName);
  return {
    icon: info.icon,
    color: colorTokenToCss(info.color),
    displayName: info.label,
    mcpNamespace: info.mcpNamespace,
    tier: info.tier,
  };
}

function buildTimelineSeam(item: AgentSessionProjectionItem): TimelineSeam {
  const childOrigin = item.origin_label || "Local";
  const parentOrigin = item.parent_origin_label || "earlier sync";

  if (item.continuation_kind === "cloud" && item.parent_continuation_kind === "cloud") {
    return {
      key: `seam:${item.session_id}:${item.timestamp}`,
      sessionId: item.session_id,
      label: "Continuation begins",
      description: "Everything below branches from the earlier continuation at this saved split point.",
      timestamp: item.timestamp,
    };
  }

  if (item.continuation_kind === "cloud") {
    return {
      key: `seam:${item.session_id}:${item.timestamp}`,
      sessionId: item.session_id,
      label: "Continuation begins",
      description: `Synced ${parentOrigin} history above. New continuation messages below.`,
      timestamp: item.timestamp,
    };
  }

  return {
    key: `seam:${item.session_id}:${item.timestamp}`,
    sessionId: item.session_id,
    label: `${childOrigin} branch begins`,
    description: `Everything below continues on ${childOrigin} from the saved split point in ${parentOrigin}.`,
    timestamp: item.timestamp,
  };
}

export function parseLonghouseOutput(
  text: string,
): {
  wallTime: string | null;
  exitCode: number | null;
  output: string;
} | null {
  const normalized = text.replace(/\r\n/g, "\n");
  const lines = normalized.split("\n");
  let index = 0;
  let sawWrapperMetadata = false;
  let wallTime: string | null = null;
  let exitCode: number | null = null;

  if (lines[index]?.startsWith("Chunk ID: ")) {
    sawWrapperMetadata = true;
    index += 1;
  }

  const wallMatch = lines[index]?.match(/^Wall time: ([\d.]+) seconds$/);
  if (wallMatch) {
    sawWrapperMetadata = true;
    wallTime = `${wallMatch[1]}s`;
    index += 1;
  }

  const exitMatch = lines[index]?.match(/^Process exited with code (\d+)$/);
  if (exitMatch) {
    sawWrapperMetadata = true;
    exitCode = parseInt(exitMatch[1], 10);
    index += 1;
  }

  if (/^Original token count: \d+$/.test(lines[index] ?? "")) {
    sawWrapperMetadata = true;
    index += 1;
  }

  if (lines[index] !== "Output:" || !sawWrapperMetadata) {
    return null;
  }

  return {
    wallTime,
    exitCode,
    output: lines.slice(index + 1).join("\n"),
  };
}

const DROPPED_TOOL_AGE_MS = 3600 * 1000;

function nonEmptyText(value: string | null | undefined): string {
  return (value || "").trim();
}

export function shouldRenderTranscriptPreview(
  preview: AgentSession["transcript_preview"] | null | undefined,
): boolean {
  return Boolean(preview && nonEmptyText(preview.text) && !preview.is_stale);
}

export function projectionItemsWithTranscriptPreview(
  projectionItems: AgentSessionProjectionItem[],
  session: AgentSession | null | undefined,
): AgentSessionProjectionItem[] {
  const preview = session?.transcript_preview;
  const previewText = nonEmptyText(preview?.text);
  if (!session || !preview || !shouldRenderTranscriptPreview(preview)) {
    return projectionItems;
  }
  const previewTimestamp = preview.timestamp;
  if (!previewTimestamp) {
    return projectionItems;
  }

  const durableEvents = projectionItems
    .map((item) => (item.kind === "event" ? item.event : null))
    .filter((event): event is AgentEvent => Boolean(event));

  const lastDurableAssistant = [...durableEvents]
    .reverse()
    .find((event) => event.role === "assistant" && nonEmptyText(event.content_text));
  if (lastDurableAssistant && nonEmptyText(lastDurableAssistant.content_text) === previewText) {
    return projectionItems;
  }

  const latestDurable = durableEvents[durableEvents.length - 1];
  const previewAt = Date.parse(previewTimestamp);
  const latestDurableAt = latestDurable ? Date.parse(latestDurable.timestamp) : Number.NaN;
  if (!Number.isNaN(previewAt) && !Number.isNaN(latestDurableAt) && latestDurableAt >= previewAt) {
    return projectionItems;
  }

  return [
    ...projectionItems,
    {
      kind: "event",
      session_id: session.id,
      timestamp: previewTimestamp,
      event: {
        id: -Math.abs(preview.event_id),
        role: "assistant",
        content_text: previewText,
        tool_name: null,
        tool_input_json: null,
        tool_output_text: null,
        tool_call_id: null,
        timestamp: previewTimestamp,
        in_active_context: true,
        is_head_branch: true,
      },
    },
  ];
}

export function isToolInteractionDropped(
  interaction: ToolInteraction,
  sessionEnded: boolean,
  now: number = Date.now(),
): boolean {
  if (interaction.resultEvent) return false;
  if (interaction.pairing === "orphan") return false;
  if (sessionEnded) return true;
  const ts = interaction.callEvent?.timestamp ?? interaction.timestamp;
  const callMs = parseUTC(ts).getTime();
  if (Number.isNaN(callMs)) return false;
  return now - callMs > DROPPED_TOOL_AGE_MS;
}

export function getToolDuration(callEvent: AgentEvent | null, resultEvent: AgentEvent | null): string | null {
  if (!callEvent || !resultEvent) return null;

  const diffMs = parseUTC(resultEvent.timestamp).getTime() - parseUTC(callEvent.timestamp).getTime();
  if (diffMs <= 0) return null;
  if (diffMs < 1000) return `${diffMs}ms`;
  return `${(diffMs / 1000).toFixed(1)}s`;
}

/**
 * Short one-line label for a tool call. Prefers the most semantic input field
 * (file path, command, query) over raw JSON.
 */
export function getToolSummary(interaction: ToolInteraction): string {
  const { callEvent, resultEvent } = interaction;

  if (callEvent?.tool_input_json) {
    const input = callEvent.tool_input_json;
    if ("description" in input && "prompt" in input) return String(input.description).slice(0, 120);
    if ("file_path" in input) return truncatePath(String(input.file_path));
    if ("command" in input) return String(input.command).slice(0, 120);
    if ("cmd" in input) return String(input.cmd).slice(0, 120);
    if ("pattern" in input) return String(input.pattern);
    if ("query" in input) return String(input.query).slice(0, 120);
    if ("path" in input) return truncatePath(String(input.path));
    if ("url" in input) return String(input.url).slice(0, 120);
    if ("prompt" in input) return String(input.prompt).slice(0, 120);
    if ("key" in input) return String(input.key).slice(0, 120);
  }

  if (resultEvent?.tool_output_text) {
    const parsed = parseLonghouseOutput(resultEvent.tool_output_text);
    const raw = parsed ? parsed.output : resultEvent.tool_output_text;
    return raw.slice(0, 120).replace(/\n/g, " ");
  }

  return "";
}

export function getToolExitCode(interaction: ToolInteraction): number | null {
  if (!interaction.resultEvent?.tool_output_text) return null;
  return parseLonghouseOutput(interaction.resultEvent.tool_output_text)?.exitCode ?? null;
}

export function buildTimelineModel(projectionItems: AgentSessionProjectionItem[]): TimelineModel {
  const byCallId = new Map<string, ToolInteraction>();
  const byCallEventId = new Map<number, ToolInteraction>();
  const fifoQueue: ToolInteraction[] = [];
  const absorbedResultIds = new Set<number>();
  const eventIdToSelectionKey = new Map<number, string>();
  const events: AgentEvent[] = [];

  // Pass 1: collect events and pair assistant→tool_result by tool_call_id.
  for (const projectionItem of projectionItems) {
    if (projectionItem.kind !== "event" || !projectionItem.event) continue;
    const event = projectionItem.event;
    events.push(event);

    if (event.role === "assistant" && event.tool_name) {
      const key = event.tool_call_id ? `id:${event.tool_call_id}` : `call:${event.id}`;
      const interaction: ToolInteraction = {
        key,
        toolName: event.tool_name,
        callEvent: event,
        resultEvent: null,
        pairing: event.tool_call_id ? "id" : "pending",
        anchorId: event.id,
        timestamp: event.timestamp,
      };

      byCallEventId.set(event.id, interaction);
      eventIdToSelectionKey.set(event.id, `tool:${interaction.key}`);

      if (event.tool_call_id) {
        byCallId.set(event.tool_call_id, interaction);
      } else {
        fifoQueue.push(interaction);
      }
    } else if (event.role === "tool") {
      let matched: ToolInteraction | undefined;

      if (event.tool_call_id) {
        matched = byCallId.get(event.tool_call_id);
      } else {
        matched = fifoQueue.shift();
        if (matched) matched.pairing = "fifo";
      }

      if (matched) {
        matched.resultEvent = event;
        absorbedResultIds.add(event.id);
        eventIdToSelectionKey.set(event.id, `tool:${matched.key}`);
      } else {
        eventIdToSelectionKey.set(event.id, `tool:orphan:${event.id}`);
      }
    } else {
      eventIdToSelectionKey.set(event.id, `message:${event.id}`);
    }
  }

  // Pass 2: flatten projection into a linear timeline of individual items.
  const items: TimelineItem[] = [];
  const toolItems: ToolInteraction[] = [];

  for (const projectionItem of projectionItems) {
    if (projectionItem.kind === "seam") {
      items.push({ kind: "seam", seam: buildTimelineSeam(projectionItem) });
      continue;
    }

    const event = projectionItem.event;
    if (!event) continue;
    if (event.role === "system") continue;
    if (event.role === "tool" && absorbedResultIds.has(event.id)) continue;

    if (event.role === "user") {
      items.push({ kind: "message", event });
      continue;
    }

    if (event.role === "assistant" && event.tool_name) {
      const interaction = byCallEventId.get(event.id);
      if (!interaction) continue;
      items.push({ kind: "tool", interaction });
      toolItems.push(interaction);
      continue;
    }

    if (event.role === "tool") {
      const interaction: ToolInteraction = {
        key: `orphan:${event.id}`,
        toolName: event.tool_name ?? "tool",
        callEvent: null,
        resultEvent: event,
        pairing: "orphan",
        anchorId: event.id,
        timestamp: event.timestamp,
      };
      items.push({ kind: "tool", interaction });
      toolItems.push(interaction);
      continue;
    }

    items.push({ kind: "message", event });
  }

  // Pass 3: collapse runs of 2+ consecutive noise-tier tools into a single
  // `noise_group` row. Context and action tools are boundaries (they keep
  // their own row). A single noise call also stays as a tool row — one row
  // is already compact enough; no point hiding it behind a chip.
  const groupedItems: TimelineItem[] = [];
  const noiseGroups: NoiseGroup[] = [];
  const groupByInteractionKey = new Map<string, NoiseGroup>();

  let buffer: ToolInteraction[] = [];

  const flush = () => {
    if (buffer.length === 0) return;
    if (buffer.length === 1) {
      groupedItems.push({ kind: "tool", interaction: buffer[0] });
    } else {
      const group: NoiseGroup = {
        key: `noise:${buffer[0].anchorId}`,
        interactions: [...buffer],
        timestamp: buffer[0].timestamp,
        anchorId: buffer[0].anchorId,
      };
      groupedItems.push({ kind: "noise_group", group });
      noiseGroups.push(group);
      for (const interaction of buffer) {
        groupByInteractionKey.set(interaction.key, group);
      }
    }
    buffer = [];
  };

  for (const item of items) {
    if (item.kind === "tool" && getToolTier(item.interaction) === "noise") {
      buffer.push(item.interaction);
      continue;
    }
    flush();
    groupedItems.push(item);
  }
  flush();

  // Pass 4: selection map.
  const selectionMap = new Map<string, TimelineSelection>();
  const eventIdToRowId = new Map<number, string>();

  for (const item of groupedItems) {
    if (item.kind === "seam") continue;

    if (item.kind === "message") {
      const key = `message:${item.event.id}`;
      const rowId = `event-${item.event.id}`;
      selectionMap.set(key, { kind: "message", key, rowId, event: item.event });
      eventIdToRowId.set(item.event.id, rowId);
      continue;
    }

    if (item.kind === "tool") {
      const key = `tool:${item.interaction.key}`;
      const rowId = `event-${item.interaction.anchorId}`;
      selectionMap.set(key, {
        kind: "tool",
        key,
        rowId,
        interaction: item.interaction,
        parentGroupKey: null,
      });
      if (item.interaction.callEvent) eventIdToRowId.set(item.interaction.callEvent.id, rowId);
      if (item.interaction.resultEvent) eventIdToRowId.set(item.interaction.resultEvent.id, rowId);
      continue;
    }

    const groupKey = `group:${item.group.key}`;
    const rowId = `event-${item.group.anchorId}`;
    selectionMap.set(groupKey, { kind: "noise_group", key: groupKey, rowId, group: item.group });

    for (const interaction of item.group.interactions) {
      const key = `tool:${interaction.key}`;
      selectionMap.set(key, {
        kind: "tool",
        key,
        rowId,
        interaction,
        parentGroupKey: item.group.key,
      });
      if (interaction.callEvent) eventIdToRowId.set(interaction.callEvent.id, rowId);
      if (interaction.resultEvent) eventIdToRowId.set(interaction.resultEvent.id, rowId);
    }
  }

  for (const [eventId, selectionKey] of eventIdToSelectionKey.entries()) {
    if (!eventIdToRowId.has(eventId)) {
      const selection = selectionMap.get(selectionKey);
      if (selection) {
        eventIdToRowId.set(eventId, selection.rowId);
        continue;
      }
      const orphanGroup = selectionKey.startsWith("tool:")
        ? groupByInteractionKey.get(selectionKey.slice("tool:".length))
        : null;
      if (orphanGroup) {
        eventIdToRowId.set(eventId, `event-${orphanGroup.anchorId}`);
      }
    }
  }

  return {
    events,
    items: groupedItems,
    toolItems,
    noiseGroups,
    selectionMap,
    eventIdToSelectionKey,
    eventIdToRowId,
  };
}

export function getPreferredSelectionKey(item: TimelineItem): string | null {
  if (item.kind === "seam") return null;
  if (item.kind === "message") return `message:${item.event.id}`;
  if (item.kind === "tool") return `tool:${item.interaction.key}`;
  return `group:${item.group.key}`;
}

export function timelineItemContainsSelection(item: TimelineItem, selectionKey: string | null): boolean {
  if (!selectionKey) return false;
  if (item.kind === "seam") return false;
  if (item.kind === "message") return selectionKey === `message:${item.event.id}`;
  if (item.kind === "tool") return selectionKey === `tool:${item.interaction.key}`;
  if (selectionKey === `group:${item.group.key}`) return true;
  return item.group.interactions.some((interaction) => selectionKey === `tool:${interaction.key}`);
}
