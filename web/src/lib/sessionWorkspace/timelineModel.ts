import type { AgentEvent, AgentEventId, AgentSession, AgentSessionProjectionItem } from "../../services/api/agents";
import { parseUTC } from "../dateUtils";
import type {
  NoiseGroup,
  TimelineAction,
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
  toolAggregate,
  toolTier,
  type ResolvedToolInfo,
  type ToolAggregate,
  type ToolTier,
} from "./toolTiers.generated";

/** Latest completed exploration calls shown when a run is expanded. */
export const EXPLORATION_OVERFLOW_VISIBLE = 8;

const AGGREGATE_SUMMARY_ORDER: ToolAggregate[] = ["search", "read", "list"];
const AGGREGATE_SUMMARY_LABEL: Record<ToolAggregate, string> = {
  search: "Searched",
  read: "Read",
  list: "Listed",
};

export function isOutsideActiveContext(event: AgentEvent | null | undefined): boolean {
  return event?.in_active_context === false;
}

export function isAgentToolInteraction(interaction: ToolInteraction): boolean {
  return interaction.toolName.toLowerCase() === "agent";
}

export function getToolTier(interaction: ToolInteraction): ToolTier {
  return toolTier(interaction.toolName);
}

/** Completed calls with an explicit aggregate category may join exploration runs. */
export function isExplorationEligible(interaction: ToolInteraction): boolean {
  if (toolAggregate(interaction.toolName) == null) return false;
  if (interaction.pairing === "orphan" || interaction.pairing === "pending") return false;
  if (!interaction.resultEvent) return false;
  if (isToolInteractionDropped(interaction) || isToolInteractionRunning(interaction)) return false;
  return true;
}

/** Header copy: `Searched 5 · Read 14 · Listed 1` (omit zero categories). */
export function formatExplorationSummary(interactions: ToolInteraction[]): string {
  const counts: Record<ToolAggregate, number> = { search: 0, read: 0, list: 0 };
  for (const interaction of interactions) {
    const category = toolAggregate(interaction.toolName);
    if (category) counts[category] += 1;
  }
  return AGGREGATE_SUMMARY_ORDER.filter((category) => counts[category] > 0)
    .map((category) => `${AGGREGATE_SUMMARY_LABEL[category]} ${counts[category]}`)
    .join(" · ");
}

export function splitExplorationOverflow<T>(
  interactions: T[],
  visible = EXPLORATION_OVERFLOW_VISIBLE,
): { earlier: T[]; latest: T[] } {
  if (interactions.length <= visible) {
    return { earlier: [], latest: interactions };
  }
  return {
    earlier: interactions.slice(0, interactions.length - visible),
    latest: interactions.slice(interactions.length - visible),
  };
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

function actionLabel(kind: string): string {
  if (kind === "turn_interrupted") return "User interrupted the turn";
  return "Session action";
}

function buildTimelineAction(item: AgentSessionProjectionItem): TimelineAction | null {
  if (!item.action) return null;
  return {
    key: item.action.id || `action:${item.session_id}:${item.timestamp}`,
    action: item.action,
    label: actionLabel(item.action.kind),
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

  const syntheticId = preview.tool_name
    ? -Math.max(2, Math.abs(preview.event_id) * 2)
    : -Math.abs(preview.event_id);
  const callItem: AgentSessionProjectionItem = {
      kind: "event",
      session_id: session.id,
      timestamp: previewTimestamp,
      event: {
        id: syntheticId,
        role: preview.role || "assistant",
        content_text: preview.tool_name ? null : previewText,
        tool_name: preview.tool_name || null,
        tool_input_json: preview.tool_input_json || null,
        tool_output_text: null,
        tool_call_id: preview.tool_call_id || null,
        tool_call_state: preview.tool_call_state || null,
        timestamp: previewTimestamp,
        in_active_context: true,
        is_head_branch: true,
      },
    };
  if (!preview.tool_name || !preview.tool_output_text || preview.tool_call_state === "running") {
    return [...projectionItems, callItem];
  }
  const resultItem: AgentSessionProjectionItem = {
    kind: "event",
    session_id: session.id,
    timestamp: previewTimestamp,
    event: {
      ...callItem.event!,
      id: syntheticId - 1,
      role: "tool",
      content_text: null,
      tool_input_json: null,
      tool_output_text: preview.tool_output_text,
    },
  };
  return [...projectionItems, callItem, resultItem];
}

export function isToolInteractionDropped(interaction: ToolInteraction): boolean {
  return interaction.callEvent?.tool_call_state === "dropped";
}

export function isToolInteractionRunning(interaction: ToolInteraction): boolean {
  return interaction.callEvent?.tool_call_state === "running";
}

export function getToolInputRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export function formatToolInput(value: unknown): string | null {
  if (value == null) return null;
  if (typeof value === "string") return value;
  const serialized = JSON.stringify(value, null, 2);
  return serialized ?? String(value);
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
    const input = getToolInputRecord(callEvent.tool_input_json);
    if (!input) return (formatToolInput(callEvent.tool_input_json) ?? "").slice(0, 120).replace(/\n/g, " ");
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
  const byCallEventId = new Map<AgentEventId, ToolInteraction>();
  const fifoQueue: ToolInteraction[] = [];
  const absorbedResultIds = new Set<AgentEventId>();
  const eventIdToSelectionKey = new Map<AgentEventId, string>();
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

    if (projectionItem.kind === "action") {
      const action = buildTimelineAction(projectionItem);
      if (action) items.push({ kind: "action", action });
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
      // Prose co-located on a tool-call event is a visible boundary (matches iOS).
      if ((event.content_text || "").trim()) {
        items.push({
          kind: "message",
          event: { ...event, tool_name: null, tool_input_json: null, tool_call_id: null },
        });
      }
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

  // Pass 3: collapse runs of 2+ consecutive exploration-eligible tools into a
  // single `noise_group` row (exploration run). Eligibility comes from
  // tool-tiers `aggregate`, not display tier — so Reads may join Greps while
  // singleton Reads stay as context one-liners. Action/web/prose/user/seam
  // boundaries flush the buffer.
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
    if (item.kind === "tool" && isExplorationEligible(item.interaction)) {
      buffer.push(item.interaction);
      continue;
    }
    flush();
    groupedItems.push(item);
  }
  flush();

  // Pass 4: selection map.
  const selectionMap = new Map<string, TimelineSelection>();
  const eventIdToRowId = new Map<AgentEventId, string>();

  for (const item of groupedItems) {
    if (item.kind === "seam" || item.kind === "action") continue;

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
  if (item.kind === "action") return null;
  if (item.kind === "message") return `message:${item.event.id}`;
  if (item.kind === "tool") return `tool:${item.interaction.key}`;
  return `group:${item.group.key}`;
}

export function timelineItemContainsSelection(item: TimelineItem, selectionKey: string | null): boolean {
  if (!selectionKey) return false;
  if (item.kind === "seam") return false;
  if (item.kind === "action") return false;
  if (item.kind === "message") return selectionKey === `message:${item.event.id}`;
  if (item.kind === "tool") return selectionKey === `tool:${item.interaction.key}`;
  if (selectionKey === `group:${item.group.key}`) return true;
  return item.group.interactions.some((interaction) => selectionKey === `tool:${interaction.key}`);
}
