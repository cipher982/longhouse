import type { AgentEvent, AgentSessionProjectionItem } from "../../services/api/agents";
import { parseUTC } from "../dateUtils";
import type {
  TimelineItem,
  TimelineModel,
  TimelineSeam,
  TimelineSelection,
  ToolBatch,
  ToolInteraction,
} from "./types";
import { truncatePath } from "./formatters";

export function isOutsideActiveContext(event: AgentEvent | null | undefined): boolean {
  return event?.in_active_context === false;
}

export function isAgentToolInteraction(interaction: ToolInteraction): boolean {
  return interaction.toolName.toLowerCase() === "agent";
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

function parseMcpTool(name: string): { namespace: string; method: string } | null {
  const parts = name.split("__");
  if (parts.length === 3 && parts[0] === "mcp") {
    return { namespace: parts[1], method: parts[2] };
  }
  return null;
}

export function getToolDisplayInfo(
  toolName: string,
): { icon: string; color: string; displayName: string; mcpNamespace?: string } {
  const mcp = parseMcpTool(toolName);
  if (mcp) {
    const namespace = mcp.namespace.toLowerCase();
    if (namespace.includes("longhouse") || namespace.includes("life-hub")) {
      return {
        icon: "O",
        color: "var(--color-brand-primary)",
        displayName: mcp.method,
        mcpNamespace: mcp.namespace,
      };
    }
    if (namespace.includes("browser")) {
      return {
        icon: "B",
        color: "var(--color-neon-cyan)",
        displayName: mcp.method,
        mcpNamespace: mcp.namespace,
      };
    }
    if (namespace.includes("search") || namespace.includes("web")) {
      return {
        icon: "S",
        color: "var(--color-neon-secondary)",
        displayName: mcp.method,
        mcpNamespace: mcp.namespace,
      };
    }
    if (namespace.includes("gdrive") || namespace.includes("gmail")) {
      return {
        icon: "G",
        color: "var(--color-intent-success)",
        displayName: mcp.method,
        mcpNamespace: mcp.namespace,
      };
    }
    return {
      icon: "M",
      color: "var(--color-text-secondary)",
      displayName: mcp.method,
      mcpNamespace: mcp.namespace,
    };
  }

  switch (toolName.toLowerCase()) {
    case "agent":
      return { icon: "A", color: "var(--color-text-tertiary)", displayName: "Agent" };
    case "bash":
    case "exec_command":
    case "shell":
    case "shell_command":
    case "run_shell_command":
    case "write_stdin":
      return { icon: "$", color: "var(--color-intent-warning)", displayName: toolName };
    case "read":
    case "read_file":
      return { icon: "R", color: "var(--color-neon-cyan)", displayName: toolName };
    case "write":
    case "create_file":
      return { icon: "W", color: "var(--color-intent-success)", displayName: toolName };
    case "edit":
    case "str_replace_editor":
      return { icon: "E", color: "var(--color-brand-primary)", displayName: toolName };
    case "grep":
      return { icon: "~", color: "var(--color-text-secondary)", displayName: toolName };
    case "glob":
      return { icon: "*", color: "var(--color-text-secondary)", displayName: toolName };
    case "task":
      return { icon: "T", color: "var(--color-neon-secondary)", displayName: toolName };
    case "todowrite":
    case "update_plan":
      return { icon: "+", color: "var(--color-brand-accent)", displayName: toolName };
    default:
      return {
        icon: (toolName[0] || " ").toUpperCase(),
        color: "var(--color-text-secondary)",
        displayName: toolName,
      };
  }
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

export function getToolDuration(callEvent: AgentEvent | null, resultEvent: AgentEvent | null): string | null {
  if (!callEvent || !resultEvent) return null;

  const diffMs = parseUTC(resultEvent.timestamp).getTime() - parseUTC(callEvent.timestamp).getTime();
  if (diffMs <= 0) return null;
  if (diffMs < 1000) return `${diffMs}ms`;
  return `${(diffMs / 1000).toFixed(1)}s`;
}

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
      }
      if (!matched) {
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

  const items: TimelineItem[] = [];
  const toolItems: ToolInteraction[] = [];

  for (const projectionItem of projectionItems) {
    if (projectionItem.kind === "seam") {
      items.push({ kind: "seam", seam: buildTimelineSeam(projectionItem) });
      continue;
    }

    const event = projectionItem.event;
    if (!event) continue;

    // Hide compaction metadata events (internal transcript bookkeeping, not user-facing)
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

  const groupedItems: TimelineItem[] = [];
  const toolBatches: ToolBatch[] = [];
  const batchByInteractionKey = new Map<string, ToolBatch>();
  const batchWindowMs = 1000;
  let index = 0;

  while (index < items.length) {
    const item = items[index];
    if (item.kind !== "tool") {
      groupedItems.push(item);
      index += 1;
      continue;
    }

    const batchTimestamp = parseUTC(item.interaction.timestamp).getTime();
    const interactions: ToolInteraction[] = [item.interaction];
    let nextIndex = index + 1;

    while (nextIndex < items.length && items[nextIndex].kind === "tool") {
      const nextItem = items[nextIndex] as { kind: "tool"; interaction: ToolInteraction };
      const nextTimestamp = parseUTC(nextItem.interaction.timestamp).getTime();
      if (nextTimestamp - batchTimestamp > batchWindowMs) break;
      interactions.push(nextItem.interaction);
      nextIndex += 1;
    }

    if (interactions.length >= 2) {
      const batch: ToolBatch = {
        key: `batch:${interactions[0].anchorId}`,
        interactions,
        timestamp: item.interaction.timestamp,
        anchorId: interactions[0].anchorId,
      };
      groupedItems.push({ kind: "tool_batch", batch });
      toolBatches.push(batch);
      for (const interaction of interactions) {
        batchByInteractionKey.set(interaction.key, batch);
      }
    } else {
      groupedItems.push(item);
    }

    index = nextIndex;
  }

  const selectionMap = new Map<string, TimelineSelection>();
  const eventIdToRowId = new Map<number, string>();

  for (const item of groupedItems) {
    if (item.kind === "seam") {
      continue;
    }

    if (item.kind === "message") {
      const key = `message:${item.event.id}`;
      const rowId = `event-${item.event.id}`;
      selectionMap.set(key, {
        kind: "message",
        key,
        rowId,
        event: item.event,
      });
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
        parentBatchKey: null,
      });
      if (item.interaction.callEvent) {
        eventIdToRowId.set(item.interaction.callEvent.id, rowId);
      }
      if (item.interaction.resultEvent) {
        eventIdToRowId.set(item.interaction.resultEvent.id, rowId);
      }
      continue;
    }

    const batchKey = `batch:${item.batch.key}`;
    const rowId = `event-${item.batch.anchorId}`;
    selectionMap.set(batchKey, {
      kind: "tool_batch",
      key: batchKey,
      rowId,
      batch: item.batch,
    });

    for (const interaction of item.batch.interactions) {
      const key = `tool:${interaction.key}`;
      selectionMap.set(key, {
        kind: "tool",
        key,
        rowId,
        interaction,
        parentBatchKey: item.batch.key,
      });

      if (interaction.callEvent) {
        eventIdToRowId.set(interaction.callEvent.id, rowId);
      }
      if (interaction.resultEvent) {
        eventIdToRowId.set(interaction.resultEvent.id, rowId);
      }
    }
  }

  for (const [eventId, selectionKey] of eventIdToSelectionKey.entries()) {
    if (!eventIdToRowId.has(eventId)) {
      const selection = selectionMap.get(selectionKey);
      if (selection) {
        eventIdToRowId.set(eventId, selection.rowId);
        continue;
      }

      const orphanBatch = selectionKey.startsWith("tool:")
        ? batchByInteractionKey.get(selectionKey.slice("tool:".length))
        : null;
      if (orphanBatch) {
        eventIdToRowId.set(eventId, `event-${orphanBatch.anchorId}`);
      }
    }
  }

  return {
    events,
    items: groupedItems,
    toolItems,
    toolBatches,
    selectionMap,
    eventIdToSelectionKey,
    eventIdToRowId,
  };
}

export function getPreferredSelectionKey(item: TimelineItem): string | null {
  if (item.kind === "seam") return null;
  if (item.kind === "message") return `message:${item.event.id}`;
  if (item.kind === "tool") return `tool:${item.interaction.key}`;
  return `batch:${item.batch.key}`;
}

export function timelineItemContainsSelection(item: TimelineItem, selectionKey: string | null): boolean {
  if (!selectionKey) return false;
  if (item.kind === "seam") return false;

  if (item.kind === "message") {
    return selectionKey === `message:${item.event.id}`;
  }

  if (item.kind === "tool") {
    return selectionKey === `tool:${item.interaction.key}`;
  }

  if (selectionKey === `batch:${item.batch.key}`) return true;
  return item.batch.interactions.some((interaction) => selectionKey === `tool:${interaction.key}`);
}
