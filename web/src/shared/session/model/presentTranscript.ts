/**
 * One presentation model for the iOS transcript embed.
 *
 * `presentTranscript` runs the shared `buildTimelineModel` over served
 * projection items and maps the result to the embed's row contract
 * (`TranscriptItem`). It replaces the Swift pair `TimelineBuilder` and
 * `WebTranscriptPayloadBuilder`: row mapping, background-notification merge,
 * the long-message collapse policy, turn-end copy and output truncation live
 * here so web and the embed cannot disagree about what a transcript says.
 *
 * Row ids keep the scheme Swift used (`user:`, `prose:`, `activity:` ...)
 * because the embed and native anchoring key on them.
 *
 * Not here: optimistic submitted rows and their placement (native overlay,
 * a later step).
 */

import type {
  AgentEvent,
  AgentEventMediaRef,
  AgentSessionProjectionItem,
  AgentToolPresentation,
  SessionEventBody,
} from "@/shared/api/agents";
import type {
  DiffLine as EmbedDiffLine,
  MediaRef,
  Subagent as EmbedSubagent,
  ToolCall,
  TranscriptItem,
  TurnEnd,
} from "@/embeds/ios-transcript/types";
import { collapseUnchanged, lineDiff } from "./diff";
import { getEditStat, formatEditStat } from "./editSummary";
import { formatTurnDuration, splitAttachmentsBlock } from "./formatters";
import { flattenSubagentTree, subagentLabel, summarizeSubagents } from "./subagents";
import {
  buildTimelineModel,
  formatActivitySummary,
  getFailurePreview,
  getToolExitCode,
  getToolIntentLabel,
  isEditInteraction,
  isToolInteractionFailed,
  messagePhase,
  narrationTrail,
  timelineItemIdentity,
} from "./timelineModel";
import { resolveToolInfo } from "./toolTiers.generated";
import type { ActivityGroup, SubagentChild, TimelineItem, ToolInteraction } from "./types";

/**
 * A row for the embed. `narrationTrail` is the one field the embed contract
 * does not carry yet (proposed for `TranscriptItem`): set on a tool or
 * activity row that trails under a commentary or reasoning leader.
 */
export type PresentedTranscriptItem = TranscriptItem & { narrationTrail?: boolean };

/** Full tool bodies native loaded for rows a lite page cut, keyed by cursor. */
export interface LiteBodyState {
  bodies?: ReadonlyMap<string, SessionEventBody>;
  loading?: ReadonlySet<string>;
  failed?: ReadonlySet<string>;
  unavailable?: ReadonlySet<string>;
}

export interface PresentTranscriptInput {
  items: AgentSessionProjectionItem[];
  subagents?: readonly SubagentChild[];
  /** Resolves relative media URLs. */
  serverUrl?: string | null;
  liteBodies?: LiteBodyState;
  /** Clock for turn-end copy ("Turn finished 9:15 AM"). */
  now?: Date;
  /** Locale and zone for turn-end copy; the device's own when omitted. */
  locale?: string;
  timeZone?: string;
}

// -- Limits (from TranscriptTextPolicy and the payload builder) ---------------

const MESSAGE_COLLAPSE_LINE_LIMIT = 600;
const MESSAGE_PREVIEW_HEAD_LINES = 220;
const MESSAGE_PREVIEW_TAIL_LINES = 80;
/** A lite preview is already the collapsed row's 2+8 lines, cut one past this. */
const PREVIEW_MAX_CHARACTERS = 4_096;
const FULL_OUTPUT_MAX_CHARACTERS = 12_000;

// -- Swift-compatible JSON text -----------------------------------------------
//
// `input` is shown to people, and the embed has always received Foundation's
// pretty form (sorted keys, `" : "`, `\/` for slashes). Kept byte-for-byte so
// the Swift golden stays the oracle; change here if the form should change.

function jsonString(value: string): string {
  return JSON.stringify(value).replace(/\//g, "\\/");
}

function prettyJsonText(value: unknown, indent: string): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "string") return jsonString(value);
  if (typeof value === "number" || typeof value === "boolean") return JSON.stringify(value);
  const inner = `${indent}  `;
  if (Array.isArray(value)) {
    if (value.length === 0) return `[\n\n${indent}]`;
    return `[\n${value.map((entry) => `${inner}${prettyJsonText(entry, inner)}`).join(",\n")}\n${indent}]`;
  }
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record).sort();
  if (keys.length === 0) return `{\n\n${indent}}`;
  const lines = keys.map((key) => `${inner}${jsonString(key)} : ${prettyJsonText(record[key], inner)}`);
  return `{\n${lines.join(",\n")}\n${indent}}`;
}

function compactJsonText(value: unknown): string {
  return JSON.stringify(value).replace(/\//g, "\\/");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** An object pretty, a string as is, anything else compact; nothing for empty or absent. */
function prettyJsonValue(value: unknown): string | undefined {
  if (value === null || value === undefined) return undefined;
  if (isRecord(value)) return Object.keys(value).length === 0 ? undefined : prettyJsonText(value, "");
  if (typeof value === "string") return value;
  return compactJsonText(value);
}

// -- Small helpers ------------------------------------------------------------

function trimmed(value: string | null | undefined): string | undefined {
  const text = value?.trim();
  return text ? text : undefined;
}

function presentedToolName(event: AgentEvent): string {
  return event.tool_presentation?.tool_name || event.tool_name || "Tool";
}

/** A string for a scalar input value, as Swift's `presentedToolInputString`. */
function scalarText(value: unknown): string | undefined {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return undefined;
}

function presentedInputString(event: AgentEvent, key: string): string | undefined {
  const presented = event.tool_presentation?.tool_input_json;
  if (isRecord(presented)) {
    const text = scalarText(presented[key]);
    if (text !== undefined) return text;
  }
  return isRecord(event.tool_input_json) ? scalarText(event.tool_input_json[key]) : undefined;
}

// Swift's `Character.isNewline` set.
// eslint-disable-next-line no-control-regex
const NEWLINES = /[\n\r\u000B\u000C\u0085\u2028\u2029]+/;

function firstLine(text: string): string {
  return text.split(NEWLINES).find((line) => line.length > 0) ?? text;
}

function lastPathComponent(path: string): string {
  const stripped = path.replace(/\/+$/, "");
  if (!stripped) return path ? "/" : "";
  return stripped.slice(stripped.lastIndexOf("/") + 1);
}

function parseMs(timestamp: string | undefined): number | undefined {
  if (!timestamp) return undefined;
  const ms = Date.parse(timestamp);
  return Number.isNaN(ms) ? undefined : ms;
}

function setIf<T extends object, K extends keyof T>(target: T, key: K, value: T[K] | null | undefined): void {
  if (value !== null && value !== undefined) target[key] = value;
}

// -- Tool summary line (Swift `TimelineBuilder.inputSummary`) -----------------

const SHELL_TOOLS = new Set(["Bash", "shell", "shell_command", "exec_command", "run_shell_command"]);

function inputSummary(event: AgentEvent): string {
  const tool = presentedToolName(event);
  const presentation = event.tool_presentation;
  const children = presentation?.children ?? [];
  if (children.length > 0 && !presentation?.wrapper_recedes) {
    return `contains ${children.map((child) => child.label).join(" · ")}`;
  }
  const input = (key: string) => presentedInputString(event, key);
  if (SHELL_TOOLS.has(tool)) {
    const command = input("command") ?? input("cmd");
    return command === undefined ? "" : firstLine(command);
  }
  switch (tool) {
    case "Grep":
    case "Glob":
      return input("pattern") ?? "";
    case "Read":
    case "Edit":
    case "Write":
    case "NotebookEdit": {
      const path = input("file_path") ?? input("path");
      return path === undefined ? "" : lastPathComponent(path);
    }
    case "apply_patch": {
      const patch = input("patch");
      if (patch === undefined) return "Applied patch";
      const paths = patch
        .split("\n")
        .flatMap((line) => {
          for (const prefix of ["*** Update File: ", "*** Add File: ", "*** Delete File: "]) {
            if (line.startsWith(prefix)) return [line.slice(prefix.length).trim()];
          }
          return [];
        });
      if (paths.length === 0) return "Applied patch";
      const extra = paths.length - 1;
      return lastPathComponent(paths[0]) + (extra > 0 ? ` + ${extra} ${extra === 1 ? "file" : "files"}` : "");
    }
    case "Task":
    case "Agent":
    case "dispatch_agent": {
      const agentType = input("subagent_type");
      const description = input("description");
      if (description) {
        if (agentType && !description.toLowerCase().startsWith(`${agentType.toLowerCase()}:`)) {
          return `${agentType}: ${description}`;
        }
        return description;
      }
      const prompt = input("prompt");
      if (prompt !== undefined) {
        const line = firstLine(prompt);
        return agentType ? `${agentType}: ${line}` : line;
      }
      return agentType ?? "";
    }
    case "WebFetch":
    case "WebSearch":
      return input("url") ?? input("query") ?? "";
    default:
      return "";
  }
}

// -- Durations and output -----------------------------------------------------

function formatToolDuration(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 60)}m ${whole % 60}s`;
}

function toolDuration(call: AgentEvent | null, result: AgentEvent | null): string | undefined {
  if (!call || !result) return undefined;
  const start = parseMs(call.timestamp);
  const end = parseMs(result.timestamp);
  if (start === undefined || end === undefined) return undefined;
  return formatToolDuration(Math.max(0, end - start) / 1000);
}

function cutCharacters(text: string, max: number): string {
  return Array.from(text).slice(0, max).join("");
}

/** Output sent to the embed: bounded so one huge result cannot stall WebKit. */
function truncatedOutput(event: AgentEvent | null | undefined): string | undefined {
  const text = event?.tool_output_text;
  if (!text) return undefined;
  const isPreview = event?.tool_output_truncated === true;
  const max = isPreview ? PREVIEW_MAX_CHARACTERS : FULL_OUTPUT_MAX_CHARACTERS;
  if (text.length <= max) return text;
  return `${cutCharacters(text, max)}${isPreview ? "\n… truncated …" : "\n... truncated in iOS transcript ..."}`;
}

// -- Message text -------------------------------------------------------------

function isFence(line: string): boolean {
  const text = line.replace(/^[ \t]+/, "");
  return text.startsWith("```") || text.startsWith("~~~");
}

function visibleMessage(text: string): { body: string; collapsed: boolean } {
  const lines = text.split("\n");
  if (text === "" || lines.length <= MESSAGE_COLLAPSE_LINE_LIMIT) return { body: text, collapsed: false };
  const tailCount = Math.min(MESSAGE_PREVIEW_TAIL_LINES, Math.max(0, lines.length - MESSAGE_PREVIEW_HEAD_LINES));
  let headEnd = Math.min(MESSAGE_PREVIEW_HEAD_LINES, lines.length - tailCount);
  const tailStart = lines.length - tailCount;
  let fences = 0;
  for (let index = 0; index < headEnd; index += 1) if (isFence(lines[index])) fences += 1;
  if (fences % 2 === 1) {
    for (let index = headEnd - 1; index >= 0; index -= 1) {
      if (isFence(lines[index])) {
        headEnd = index;
        break;
      }
    }
  }
  const hidden = Math.max(0, tailStart - headEnd);
  const marker = `... ${hidden} line${hidden === 1 ? "" : "s"} hidden ...`;
  return {
    body: [...lines.slice(0, headEnd), "", marker, "", ...lines.slice(lines.length - tailCount)].join("\n"),
    collapsed: true,
  };
}

// -- Media --------------------------------------------------------------------

function absoluteMediaUrl(raw: string | null | undefined, serverUrl: string | null | undefined): string | undefined {
  const url = raw?.trim();
  if (!url) return undefined;
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(url)) return url;
  if (!serverUrl) return url;
  try {
    return new URL(url, serverUrl).href;
  } catch {
    return url;
  }
}

function mediaRefs(refs: readonly AgentEventMediaRef[], serverUrl: string | null | undefined): MediaRef[] | undefined {
  const seen = new Set<string>();
  const out: MediaRef[] = [];
  for (const ref of refs) {
    const key = `${ref.sha256}:${ref.thumb_url ?? ref.blob_url}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const imageLike = ref.mime_type ? ref.mime_type.startsWith("image/") : true;
    const visible =
      ref.media_state === "present" && imageLike
        ? absoluteMediaUrl(ref.thumb_url ?? ref.blob_url, serverUrl)
        : undefined;
    if (visible === undefined && ref.media_state === "present") continue;
    const media: MediaRef = { sha256: ref.sha256, mediaState: ref.media_state };
    setIf(media, "url", visible);
    setIf(media, "blobUrl", absoluteMediaUrl(ref.blob_url, serverUrl));
    setIf(media, "mimeType", ref.mime_type);
    setIf(media, "width", ref.width);
    setIf(media, "height", ref.height);
    out.push(media);
  }
  return out.length > 0 ? out : undefined;
}

// -- Turn-end copy ------------------------------------------------------------

interface ClockOptions {
  now: Date;
  locale?: string;
  timeZone?: string;
}

function doneAt(endedAt: string, verb: string, clock: ClockOptions): string {
  const date = parseMs(endedAt);
  if (date === undefined) return verb;
  const when = new Date(date);
  const zone = clock.timeZone ? { timeZone: clock.timeZone } : {};
  const time = when.toLocaleTimeString(clock.locale, { hour: "numeric", minute: "2-digit", ...zone });
  const dayKey = (d: Date) => d.toLocaleDateString("en-CA", zone);
  if (dayKey(when) === dayKey(clock.now)) return `${verb} ${time}`;
  const withinWeek = clock.now.getTime() - when.getTime() < 7 * 24 * 3600 * 1000;
  const day = withinWeek
    ? when.toLocaleDateString(clock.locale, { weekday: "short", ...zone })
    : when.toLocaleDateString(clock.locale, { month: "short", day: "numeric", ...zone });
  return `${verb} ${day} ${time}`;
}

function turnEndFor(item: TimelineItem, clock: ClockOptions): TurnEnd | undefined {
  let turnEnd: AgentEvent["turn_end"];
  switch (item.kind) {
    case "message":
      turnEnd = item.event.turn_end;
      break;
    case "tool":
      turnEnd = item.interaction.resultEvent?.turn_end ?? item.interaction.callEvent?.turn_end;
      break;
    case "activity_group":
      for (const call of [...item.group.interactions].reverse()) {
        turnEnd = call.resultEvent?.turn_end ?? call.callEvent?.turn_end;
        if (turnEnd) break;
      }
      break;
    default:
      return undefined;
  }
  if (!turnEnd) return undefined;
  const outcome = turnEnd.outcome?.trim().toLowerCase();
  const failed = outcome === "error" || outcome === "failed";
  const stopped = failed || outcome === "aborted" || outcome === "interrupted";
  const verb = failed ? "Failed after" : stopped ? "Interrupted after" : "Worked for";
  const base = doneAt(turnEnd.ended_at, stopped ? "stopped" : "Turn finished", clock);
  const hook = turnEnd.stop_hook_summary?.trim();
  return {
    label: `${verb} ${formatTurnDuration(turnEnd.duration_ms)}`,
    doneAt: hook ? `${base} · ${hook}` : base,
  };
}

// -- Lite bodies --------------------------------------------------------------

function liteCursor(event: AgentEvent | null | undefined): string | undefined {
  if (!event) return undefined;
  if (event.lite_body?.cursor) return event.lite_body.cursor;
  return (event.tool_input_truncated || event.tool_output_truncated) && event.cursor ? event.cursor : undefined;
}

function mergedEvent(event: AgentEvent | null, lite: LiteBodyState | undefined): AgentEvent | null {
  const cursor = liteCursor(event);
  const body = cursor ? lite?.bodies?.get(cursor) : undefined;
  if (!event || !body) return event;
  return {
    ...event,
    tool_input_json: body.tool_input_json ?? event.tool_input_json,
    tool_output_text: body.tool_output_text ?? event.tool_output_text,
    tool_presentation: body.tool_presentation ?? event.tool_presentation,
    tool_input_truncated: false,
    tool_output_truncated: false,
  };
}

function mergedInteraction(interaction: ToolInteraction, lite: LiteBodyState | undefined): ToolInteraction {
  if (!lite?.bodies?.size) return interaction;
  const callEvent = mergedEvent(interaction.callEvent, lite);
  const resultEvent = mergedEvent(interaction.resultEvent, lite);
  if (callEvent === interaction.callEvent && resultEvent === interaction.resultEvent) return interaction;
  return { ...interaction, callEvent, resultEvent, presentation: callEvent?.tool_presentation ?? interaction.presentation };
}

function bodyCursors(interactions: readonly ToolInteraction[]): string[] {
  const cursors: string[] = [];
  for (const interaction of interactions) {
    for (const event of [interaction.callEvent, interaction.resultEvent]) {
      const cursor = liteCursor(event);
      if (cursor && !cursors.includes(cursor)) cursors.push(cursor);
    }
  }
  return cursors;
}

function bodyState(cursors: readonly string[], lite: LiteBodyState | undefined): string {
  if (cursors.some((cursor) => lite?.loading?.has(cursor))) return "loading";
  if (cursors.some((cursor) => lite?.failed?.has(cursor))) return "failed";
  if (cursors.every((cursor) => lite?.unavailable?.has(cursor))) return "unavailable";
  return "preview";
}

// -- Row builders -------------------------------------------------------------

function baseRow(id: string, kind: string): PresentedTranscriptItem {
  return { id, kind, calls: [], collapsed: false };
}

function messageRow(
  id: string,
  role: "user" | "assistant",
  event: AgentEvent,
  serverUrl: string | null | undefined,
): PresentedTranscriptItem {
  const text = event.content_text ?? "";
  const split = role === "user" ? splitAttachmentsBlock(text) : { text, imageCount: 0 };
  const { body, collapsed } = visibleMessage(split.text);
  const row = baseRow(id, "message");
  row.role = role;
  row.body = body;
  if (collapsed) row.fullBody = split.text;
  row.collapsed = collapsed;
  if (role === "user") setIf(row, "origin", event.input_origin?.authored_via);
  setIf(row, "media", mediaRefs(event.media_refs ?? [], serverUrl));
  if (role === "user") {
    if (split.imageCount > 0) row.attachedImages = split.imageCount;
    const thumbs = (event.input_origin?.attachment_urls ?? [])
      .map((url) => absoluteMediaUrl(url, serverUrl))
      .filter((url): url is string => url !== undefined);
    if (thumbs.length > 0) row.attachmentThumbs = thumbs;
  } else {
    setIf(row, "phase", trimmed(messagePhase(event)));
    setIf(row, "apiError", event.api_error as Record<string, unknown> | null | undefined);
  }
  return row;
}

function notificationRow(
  id: string,
  text: string,
  extras: { origin?: string | null; wake?: boolean; event?: AgentEvent } = {},
): PresentedTranscriptItem {
  const row = baseRow(id, "providerNotification");
  row.body = text;
  if (extras.origin === "longhouse") row.origin = "longhouse";
  const event = extras.event;
  if (event) {
    setIf(row, "taskNotification", event.task_notification as Record<string, unknown> | null | undefined);
    setIf(row, "waitingBackgroundAgents", event.waiting_background_agents);
    setIf(row, "apiError", event.api_error as Record<string, unknown> | null | undefined);
  }
  if (extras.wake) row.wake = true;
  return row;
}

function flattenedSubagents(spawned: readonly SubagentChild[]): EmbedSubagent[] | undefined {
  if (spawned.length === 0) return undefined;
  const out: EmbedSubagent[] = [];
  for (const { child, depth } of flattenSubagentTree(spawned)) {
    const label = subagentLabel(child);
    const description = child.description?.trim();
    const entry: EmbedSubagent = { sessionId: child.session_id, label, toolCalls: child.tool_calls };
    setIf(entry, "agentType", child.agent_type);
    if (description && description !== label) entry.description = description;
    setIf(entry, "liveStatus", child.live_status);
    if (child.is_fork) entry.isFork = true;
    if (depth > 0) entry.depth = depth;
    setIf(entry, "durationMs", child.duration_ms);
    setIf(entry, "totalTokens", child.total_tokens);
    out.push(entry);
  }
  return out.length > 0 ? out : undefined;
}

function diffRows(patch: NonNullable<ReturnType<typeof getEditStat>["patch"]>, hasStat: boolean): EmbedDiffLine[] | undefined {
  switch (patch.kind) {
    case "create":
      return patch.content.split("\n").map((text) => ({ kind: "add", text }));
    case "delete":
      return patch.content.split("\n").map((text) => ({ kind: "remove", text }));
    case "patch":
      return patch.text.split("\n").map((text) => {
        if (text.startsWith("+") && !text.startsWith("+++")) return { kind: "add", text };
        if (text.startsWith("-") && !text.startsWith("---")) return { kind: "remove", text };
        return { kind: "equal", text };
      });
    case "replace":
      if (!hasStat) return undefined;
      return collapseUnchanged(lineDiff(patch.oldStr, patch.newStr), 2).map(({ kind, text }) => ({ kind, text }));
  }
}

interface QuestionOption {
  label: string;
  description: string | undefined;
}

function jsonText(value: unknown): string | undefined {
  return trimmed(scalarText(value));
}

function questionOption(raw: unknown): QuestionOption | undefined {
  if (isRecord(raw)) {
    const label = jsonText(raw.label) ?? jsonText(raw.value) ?? jsonText(raw.text);
    if (label === undefined) return undefined;
    return { label, description: jsonText(raw.description) ?? jsonText(raw.detail) };
  }
  const label = jsonText(raw);
  return label === undefined ? undefined : { label, description: undefined };
}

function questionsFrom(input: unknown): { header?: string; question: string; options: QuestionOption[] }[] {
  if (!isRecord(input)) return [];
  let raw: unknown[] = [];
  if (Array.isArray(input.questions)) raw = input.questions;
  else if (input.question !== undefined || input.prompt !== undefined) raw = [input];
  return raw.flatMap((entry) => {
    if (!isRecord(entry)) return [];
    const rawOptions = Array.isArray(entry.options) ? entry.options : Array.isArray(entry.choices) ? entry.choices : [];
    return [
      {
        header: jsonText(entry.header) ?? jsonText(entry.title),
        question: jsonText(entry.question) ?? jsonText(entry.prompt) ?? jsonText(entry.label) ?? "Answer required",
        options: rawOptions.flatMap((option) => questionOption(option) ?? []),
      },
    ];
  });
}

function questionRow(id: string, call: AgentEvent, hasResult: boolean): PresentedTranscriptItem {
  const questions = questionsFrom(call.tool_input_json);
  const body = questions.map((entry) => entry.question).join("\n\n");
  const row = baseRow(id, "question");
  row.title = questions[0]?.header ?? "Question";
  row.subtitle = hasResult ? "Answered in terminal" : "Answer in terminal";
  row.body = body || "Claude is waiting for your answer.";
  row.status = hasResult ? "answered" : "waiting";
  row.calls = questions
    .flatMap((entry) => entry.options)
    .map((option) => ({ title: option.label, subtitle: option.description ?? "", status: "option" }));
  return row;
}

function presentationCalls(
  presentation: AgentToolPresentation | null | undefined,
  call: AgentEvent,
  result: AgentEvent | null,
  toolName: string,
): ToolCall[] {
  const calls: ToolCall[] = (presentation?.children ?? []).map((child) => {
    const entry: ToolCall = { title: child.label, subtitle: child.tool_name, status: "parsed" };
    setIf(entry, "input", prettyJsonValue(child.tool_input_json));
    if (presentation?.wrapper_recedes) setIf(entry, "output", truncatedOutput(result));
    return entry;
  });
  if (presentation?.wrapper_recedes) {
    const raw: ToolCall = {
      title: `Raw enclosing ${presentation.source_tool_name || toolName}`,
      subtitle: "Provider evidence",
      status: "raw",
    };
    setIf(raw, "rawInput", prettyJsonValue(call.tool_input_json));
    calls.push(raw);
  }
  return calls;
}

function toolRow(
  id: string,
  interaction: ToolInteraction,
  serverUrl: string | null | undefined,
): PresentedTranscriptItem {
  const orphan = interaction.pairing === "orphan";
  const result = interaction.resultEvent;
  const call = (interaction.callEvent ?? result) as AgentEvent;
  const toolName = call.tool_name ?? "Tool";
  if (toolName === "AskUserQuestion") return questionRow(id, call, result !== null);

  const presentation = call.tool_presentation;
  const resolved = resolveToolInfo(presentedToolName(call));
  const failed = isToolInteractionFailed(interaction);
  const exitCode = getToolExitCode(interaction);
  const state = call.tool_call_state;
  let status: string | undefined;
  if (orphan) status = "orphan";
  else if (state === "running") status = "running";
  else if (state === "dropped") status = "dropped";
  else if (failed) status = exitCode != null && exitCode !== 0 ? `exit ${exitCode}` : "failed";
  else if (state === "completed") status = "done";

  const editStat = isEditInteraction(interaction) ? getEditStat(interaction) : null;
  const editLabel = editStat ? formatEditStat(editStat) : null;
  const spawned = interaction.children ?? [];
  const intent = getToolIntentLabel(interaction);
  const summary = inputSummary(call);

  const row = baseRow(id, "tool");
  row.title = presentation?.label ?? resolved.label;
  row.subtitle = editLabel ?? summary;
  if (status !== undefined) row.status = status;
  setIf(row, "duration", result ? toolDuration(call, result) : undefined);
  row.input =
    prettyJsonValue(presentation?.wrapper_recedes ? presentation.tool_input_json : call.tool_input_json) ?? summary;
  setIf(row, "output", truncatedOutput(result ?? (state === "running" ? call : null)));
  row.calls = presentationCalls(presentation, call, result, toolName);
  if (presentation?.disposition === "parsed") {
    row.origin = `Parsed via ${presentation.execution_method || presentation.source_tool_name || toolName}`;
  }
  const refs = [...(call.media_refs ?? []), ...(result?.media_refs ?? [])];
  setIf(row, "media", mediaRefs(refs, serverUrl));
  setIf(row, "failurePreview", getFailurePreview(interaction));
  if (editStat?.patch) setIf(row, "diff", diffRows(editStat.patch, editStat.hasStat));
  setIf(row, "subagents", flattenedSubagents(spawned));
  if (spawned.length > 0) row.subagentSummary = summarizeSubagents(spawned);
  setIf(row, "toolIntent", intent);
  if (state === "running") setIf(row, "startedAtMs", parseMs(call.timestamp));
  return row;
}

function activityRow(
  id: string,
  group: ActivityGroup,
  serverUrl: string | null | undefined,
): PresentedTranscriptItem {
  const calls = group.interactions;
  const summary = formatActivitySummary(calls);
  const seen = new Set<string>();
  const spawned = calls.flatMap((interaction) => interaction.children ?? []).filter((child) => {
    if (seen.has(child.session_id)) return false;
    seen.add(child.session_id);
    return true;
  });

  const childCalls: ToolCall[] = calls.map((interaction) => {
    const call = (interaction.callEvent ?? interaction.resultEvent) as AgentEvent;
    const result = interaction.resultEvent;
    const presentation = call.tool_presentation;
    const state = call.tool_call_state;
    const summaryLine = inputSummary(call);
    const entry: ToolCall = {
      title: presentation?.label ?? resolveToolInfo(presentedToolName(call)).label,
      subtitle: summaryLine,
      status: state === "running" ? "running" : state === "dropped" ? "dropped" : "done",
    };
    entry.input =
      prettyJsonValue(presentation?.wrapper_recedes ? presentation.tool_input_json : call.tool_input_json) ??
      summaryLine;
    if (presentation?.wrapper_recedes) setIf(entry, "rawInput", prettyJsonValue(call.tool_input_json));
    setIf(entry, "output", truncatedOutput(result ?? (state === "running" ? call : null)));
    setIf(entry, "media", mediaRefs([...(call.media_refs ?? []), ...(result?.media_refs ?? [])], serverUrl));
    if (state === "running") setIf(entry, "startedAtMs", parseMs(call.timestamp));
    setIf(entry, "toolIntent", trimmed(call.tool_intent));
    return entry;
  });

  const row = baseRow(id, "activityGroup");
  row.title = summary || "Activity";
  row.subtitle = String(calls.length);
  row.calls = childCalls;
  setIf(row, "subagents", flattenedSubagents(spawned));
  if (spawned.length > 0) row.subagentSummary = summarizeSubagents(spawned);
  return row;
}

// -- Row mapping --------------------------------------------------------------

function rowId(item: TimelineItem): string | null {
  switch (item.kind) {
    case "seam":
      return null;
    case "action":
      return item.action.action.id;
    case "compaction":
      return `compaction-boundary:${item.event.id}`;
    case "provider_notification":
      return `provider-notification:${item.event.id}`;
    case "reasoning":
      return `reasoning:${item.event.id}`;
    case "message":
      return `${item.event.role === "user" ? "user" : "prose"}:${item.event.id}`;
    case "tool":
      return item.interaction.callEvent
        ? `tool:${item.interaction.callEvent.id}`
        : `orphan:${item.interaction.resultEvent?.id}`;
    case "activity_group": {
      const first = item.group.interactions[0];
      return `activity:${first?.callEvent?.id ?? "missing"}`;
    }
  }
}

const THINKING_PREFIX = "Thinking:\n";

function mapItem(
  item: TimelineItem,
  id: string,
  input: PresentTranscriptInput,
  clock: ClockOptions,
): PresentedTranscriptItem | null {
  const { serverUrl, liteBodies } = input;
  let row: PresentedTranscriptItem;
  switch (item.kind) {
    case "seam":
      return null;
    case "action": {
      row = baseRow(id, "action");
      row.title = item.action.label;
      setIf(row, "subtitle", item.action.action.provider);
      row.status = item.action.action.kind;
      return row;
    }
    case "compaction":
      row = baseRow(id, "compaction");
      row.body = item.event.content_text ?? "";
      return row;
    case "provider_notification":
      return notificationRow(id, item.event.content_text ?? "", { event: item.event });
    case "reasoning": {
      const text = item.event.content_text ?? "";
      row = baseRow(id, "reasoning");
      row.body = text.startsWith(THINKING_PREFIX) ? text.slice(THINKING_PREFIX.length) : text;
      if (item.event.reasoning_withheld || item.event.item?.visibility === "withheld") row.reasoningWithheld = true;
      return row;
    }
    case "message": {
      const event = item.event;
      const origin: string | null | undefined = event.input_origin?.origin;
      if (event.role === "user" && (origin === "wake" || origin === "longhouse")) {
        row = notificationRow(id, event.content_text ?? "Background task finished", {
          origin,
          wake: origin === "wake",
        });
      } else {
        row = messageRow(id, event.role === "user" ? "user" : "assistant", event, serverUrl);
      }
      break;
    }
    case "tool": {
      const interaction = mergedInteraction(item.interaction, liteBodies);
      row = toolRow(id, interaction, serverUrl);
      attachCursors(row, [item.interaction], liteBodies);
      break;
    }
    case "activity_group": {
      const merged: ActivityGroup = {
        ...item.group,
        interactions: item.group.interactions.map((interaction) => mergedInteraction(interaction, liteBodies)),
      };
      row = activityRow(id, merged, serverUrl);
      attachCursors(row, item.group.interactions, liteBodies);
      break;
    }
  }
  const turnEnd = turnEndFor(item, clock);
  if (turnEnd) row.turnEnd = turnEnd;
  return row;
}

function attachCursors(
  row: PresentedTranscriptItem,
  interactions: readonly ToolInteraction[],
  lite: LiteBodyState | undefined,
): void {
  const cursors = bodyCursors(interactions);
  if (cursors.length === 0) return;
  row.bodyCursors = cursors;
  row.bodyState = bodyState(cursors, lite);
}

/**
 * Consecutive background completions (wake notices only) read as one row with
 * a count, placed at the latest one. Longhouse's own close and cleanup
 * notices are never merged, and a message between two completions keeps them
 * apart.
 */
export function collapsingProviderNotifications(rows: PresentedTranscriptItem[]): PresentedTranscriptItem[] {
  const out: PresentedTranscriptItem[] = [];
  let run: PresentedTranscriptItem[] = [];
  const flush = () => {
    const latest = run[run.length - 1];
    if (!latest) return;
    if (run.length === 1) {
      out.push(latest);
    } else {
      const merged = notificationRow(latest.id, `${latest.body ?? "Background task finished"} (${run.length} updates)`, {
        wake: true,
      });
      if (latest.turnEnd) merged.turnEnd = latest.turnEnd;
      out.push(merged);
    }
    run = [];
  };
  for (const row of rows) {
    if (row.wake === true) {
      run.push(row);
    } else {
      flush();
      out.push(row);
    }
  }
  flush();
  return out;
}

/** Served projection items to the embed's rows. */
export function presentTranscript(input: PresentTranscriptInput): PresentedTranscriptItem[] {
  const model = buildTimelineModel(input.items, input.subagents ?? []);
  const trail = narrationTrail(model.items);
  const clock: ClockOptions = { now: input.now ?? new Date(), locale: input.locale, timeZone: input.timeZone };
  const rows: PresentedTranscriptItem[] = [];
  for (const item of model.items) {
    const id = rowId(item);
    if (id === null) continue;
    const row = mapItem(item, id, input, clock);
    if (!row) continue;
    if (trail.has(timelineItemIdentity(item))) row.narrationTrail = true;
    rows.push(row);
  }
  return collapsingProviderNotifications(rows);
}
