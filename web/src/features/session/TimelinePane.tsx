import { Fragment, memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router";
import { EmptyState, Spinner } from "@/shared/ui";
import { FunnelIcon } from "@/shared/ui/icons";
import { ProviderGlyph } from "@/shared/ui/ProviderGlyph";
import { ProviderNoticeRow } from "./ProviderNoticeRow";
import { ReasoningRow } from "./ReasoningRow";
import {
  RunningCallRow,
  TimelineLivenessProvider,
  useRunningCallState,
  useTimelineLiveness,
  type TimelineLiveness,
} from "./RunningCallRow";
import type {
  ActivityGroup,
  SubagentChild,
  TimelineAction,
  TimelineItem,
  TimelineSeam,
  ToolInteraction,
} from "@/shared/session/model";
import {
  flattenSubagentTree,
  formatContinuationStamp,
  formatActivitySummary,
  formatCompactTokens,
  formatEditStat,
  formatToolInput,
  formatTime,
  formatTurnDuration,
  getActivityGroupTiming,
  getEditStat,
  getFailurePreview,
  getTimelineMessagePreview,
  splitAttachmentsBlock,
  withoutImageTokenLines,
  getInteractionDisplayInfo,
  getToolDuration,
  getToolExitCode,
  getToolIntentLabel,
  isToolIntentAuthored,
  isAgentLaunchInteraction,
  getToolTargetText,
  getToolOutputPreview,
  getLiveToolOutputPreview,
  countTimelineItems,
  formatTranscriptSummary,
  getToolInputRecord,
  getToolSummary,
  getToolTier,
  isAgentToolInteraction,
  isEditInteraction,
  isLivePreviewTimelineItem,
  isOutsideActiveContext,
  isToolInteractionDropped,
  isToolInteractionFailed,
  isToolInteractionRunning,
  messagePhase,
  narrationTrail,
  timelineItemIdentity,
  parseLonghouseOutput,
  splitExplorationOverflow,
  summarizeApiError,
  summarizeCompactionNotice,
  timelineItemContainsSelection,
  subagentLabel,
  summarizeSubagents,
} from "@/shared/session/model";
import { useDebouncedValue } from "@/shared/hooks/useDebouncedValue";
import { useScrollToLoad } from "./useScrollToLoad";
import { CodeFence, detectLanguage } from "./CodeFence";
import { EditDiffView } from "./EditDiffView";
import { MarkdownBody } from "./MarkdownBody";
import { OUTBOX_STATE_LABEL, OutboxRow, type OutboxEntry } from "./OutboxRow";
import { matchSendsToTranscript, type SendForMatch } from "./chat/unrecordedInputs";
import { hasLiteBodies, useFullToolInteraction, useToolBodyPrefetch } from "./liteBodies";
import type { AgentEvent, AgentEventMediaRef, AgentEventTurnEnd } from "@/shared/api/agents";
import { errorDetails } from "@/shared/ui/errorDetails";
import { parseUTC } from "@/shared/lib/dateUtils";
import { RelativeTime } from "@/shared/ui/RelativeTime";

type EventFilter = "all" | "messages" | "tools";

/** A pause this long between thoughts earns the later one a time label. */
const THOUGHT_TIME_GAP_MS = 5 * 60_000;

type TranscriptQuestionOption = {
  label: string;
  description: string | null;
};

type TranscriptQuestion = {
  id: string;
  header: string | null;
  question: string;
  options: TranscriptQuestionOption[];
};

interface TimelinePaneProps {
  items: TimelineItem[];
  totalEntries: number;
  loadedEntries: number;
  abandonedEvents: number;
  showAbandonedBranches: boolean;
  onShowAbandonedBranchesChange: (show: boolean) => void;
  hasPreviousPage: boolean;
  isFetchingPreviousPage: boolean;
  onFetchPreviousPage: () => void;
  loading?: boolean;
  error?: unknown;
  controlOnly?: boolean;
  selectedKey: string | null;
  onSelectKey: (key: string) => void;
  /** Called when local filtering hides/reveals the parent-selected key. */
  onVisibleSelectionChange?: (visibleKey: string | null) => void;
  /** Navigation / context content rendered at the start of the header bar. */
  headerLeft?: ReactNode;
  /** Live/idle state readout (dot + sentence), rendered before the filter
   *  and overflow icons at the far right of the header bar. */
  headerState?: ReactNode;
  /** Actions rendered at the far right of the header bar. */
  headerRight?: ReactNode;
  /** The app bar's page slot. When set, the header bar renders there (one
   *  top bar for the whole page) instead of above the transcript. */
  headerTarget?: HTMLElement | null;
  /** Readout rail (Phase 4 Instruments), rendered inside the transcript
   *  scroller (so wheel input over it scrolls the transcript) and pinned to
   *  its bottom-right on wide viewports; CSS hides it below 1180px. */
  rail?: ReactNode;
  /** Pinned right-hand turns drawer, rendered beside the pane when pinned. */
  outline?: ReactNode;
  /** Floating right-edge turn ticks and hover card, anchored to the transcript
   *  body viewport when turns are not pinned. */
  turnOverlay?: ReactNode;
  dock?: ReactNode;
  listRef?: (node: HTMLDivElement | null) => void;
  renderMedia?: boolean;
  /** Session provider; its glyph marks assistant turns on the transcript trace. */
  provider?: string | null;
  /** Sends the transcript has not echoed yet; rendered after the last row,
   *  except settled ones carrying `at`, which stand at that time among the rows. */
  outbox?: OutboxEntry[];
  /** Session evidence for running calls (work claim, host). Without it a
   *  running call renders as a plain row and never animates. */
  liveness?: TimelineLiveness | null;
  topBanner?: ReactNode;
  subagents?: readonly SubagentChild[];
}

function isWithheldReasoningEvent(event: AgentEvent): boolean {
  const raw = event.content_text || "";
  const cleaned = (raw.startsWith("Thinking:\n") ? raw.slice("Thinking:\n".length) : raw).trim();
  const isWithheld = Boolean(event.reasoning_withheld) || event.item?.visibility === "withheld";
  return isWithheld && (!cleaned || cleaned === "[Reasoning withheld]");
}

function tier3ItemLabel(item: TimelineItem): string | null {
  if (item.kind === "compaction") {
    return /microcompact/i.test(item.event.content_text ?? "") ? "Microcompacted" : "Compacted";
  }
  if (item.kind === "reasoning" && isWithheldReasoningEvent(item.event)) {
    return "Reasoning withheld";
  }
  if (item.kind === "provider_notification") {
    if (item.event.waiting_background_agents != null && item.event.waiting_background_agents > 0) {
      return "Waiting on agents";
    }
    if (item.event.api_error) {
      return "Retried API error";
    }
  }
  return null;
}

function isTier3MechanicalItem(item: TimelineItem, index: number, lastProgressIndex: number): boolean {
  if (item.kind === "compaction") {
    return summarizeCompactionNotice(item.event.content_text).title === "Context microcompacted";
  }
  if (item.kind === "reasoning") return isWithheldReasoningEvent(item.event);
  if (item.kind === "provider_notification") {
    if (item.event.waiting_background_agents != null && item.event.waiting_background_agents > 0) {
      return true;
    }
    if (item.event.api_error && index < lastProgressIndex) {
      return true;
    }
  }
  return false;
}

function resolveTaskNotificationSubagentId(
  taskNotif: NonNullable<AgentEvent["task_notification"]>,
  lookup: ReadonlyMap<string, string>,
): string | null {
  const taskId = typeof taskNotif.task_id === "string" ? taskNotif.task_id.trim() : "";
  if (taskId) {
    const direct = lookup.get(taskId);
    if (direct) return direct;
    const bare = taskId.startsWith("agent-") ? taskId.slice(6) : taskId;
    const fromBare = lookup.get(bare) ?? lookup.get(`agent-${bare}`);
    if (fromBare) return fromBare;
  }
  const toolUseId = typeof taskNotif.tool_use_id === "string" ? taskNotif.tool_use_id.trim() : "";
  if (toolUseId) {
    return lookup.get(`tool:${toolUseId}`) ?? null;
  }
  return null;
}

function timelineItemTimeMs(item: TimelineItem): number {
  const timestamp =
    item.kind === "seam"
      ? item.seam.timestamp
      : item.kind === "action"
        ? item.action.timestamp
        : item.kind === "tool"
          ? item.interaction.timestamp
          : item.kind === "activity_group"
            ? item.group.timestamp
            : item.event.timestamp;
  return parseUTC(timestamp).getTime();
}

/** The React key a send and its transcript echo share. */
function sendKey(clientRequestId: string): string {
  return `send:${clientRequestId}`;
}

function outboxTimeMs(entry: OutboxEntry): number {
  return entry.at ? parseUTC(entry.at).getTime() : Number.NaN;
}

function nonEmptyText(value: unknown): string | null {
  if (value == null) return null;
  const text = String(value).trim();
  return text || null;
}

function isAskUserQuestion(interaction: ToolInteraction): boolean {
  // Claude's question card parses AskUserQuestion's structured questions;
  // another provider's ask tool renders as an ordinary call until its
  // payload is mapped.
  return interaction.toolName === "AskUserQuestion";
}

function normalizeTranscriptQuestions(input: Record<string, unknown> | null | undefined): TranscriptQuestion[] {
  if (!input) return [];
  const rawQuestions = Array.isArray(input.questions)
    ? input.questions
    : input.question || input.prompt
      ? [input]
      : [];

  return rawQuestions.flatMap((raw, index): TranscriptQuestion[] => {
    if (!raw || typeof raw !== "object") return [];
    const item = raw as Record<string, unknown>;
    const rawOptions = Array.isArray(item.options)
      ? item.options
      : Array.isArray(item.choices)
        ? item.choices
        : [];
    const options = rawOptions.flatMap((option): TranscriptQuestionOption[] => {
      if (option && typeof option === "object") {
        const optionObject = option as Record<string, unknown>;
        const label = nonEmptyText(optionObject.label ?? optionObject.value ?? optionObject.text);
        if (!label) return [];
        return [
          {
            label,
            description: nonEmptyText(optionObject.description ?? optionObject.detail),
          },
        ];
      }
      const label = nonEmptyText(option);
      return label ? [{ label, description: null }] : [];
    });
    return [
      {
        id: nonEmptyText(item.id ?? item.name ?? item.key) ?? `question-${index + 1}`,
        header: nonEmptyText(item.header ?? item.title),
        question: nonEmptyText(item.question ?? item.prompt ?? item.label) ?? "Answer required",
        options,
      },
    ];
  });
}

const SeamRow = memo(function SeamRow({ seam }: { seam: TimelineSeam }) {
  return (
    <div id={`seam-${seam.key}`} className="tl-seam" data-testid="session-timeline-seam">
      <div className="tl-seam__rule" />
      <div className="tl-seam__body">
        <span className="tl-seam__label">{seam.label}</span>
        <span className="tl-seam__description">{seam.description}</span>
      </div>
      <div className="tl-seam__stamp">{formatContinuationStamp(seam.timestamp)}</div>
    </div>
  );
});

const CompactionRow = memo(function CompactionRow({ event }: { event: AgentEvent }) {
  const { title, detail } = summarizeCompactionNotice(event.content_text);
  const isMicro = title === "Context microcompacted";
  return (
    <div
      id={`event-${event.id}`}
      className={`tl-seam tl-seam--compaction${isMicro ? " tl-compaction" : ""}`}
      data-testid="session-compaction-boundary"
    >
      <div className="tl-seam__rule" />
      <div className={isMicro ? "tl-compaction__body" : "tl-seam__body"}>
        <span className={`tl-seam__label${isMicro ? " tl-compaction__label" : ""}`}>{title}</span>
        {detail ? (
          <span className={`tl-seam__description${isMicro ? " tl-compaction__detail" : ""}`}>{detail}</span>
        ) : null}
      </div>
      <div className="tl-seam__rule" />
      <div className={isMicro ? "tl-compaction__stamp" : "tl-seam__stamp"} title={formatTime(event.timestamp)}>
        {formatContinuationStamp(event.timestamp)}
      </div>
    </div>
  );
});

function timelineItemRowId(item: TimelineItem): string | null {
  switch (item.kind) {
    case "seam":
      return `seam-${item.seam.key}`;
    case "action":
      return `action-${item.action.action.event_id ?? item.action.key}`;
    case "compaction":
    case "reasoning":
    case "provider_notification":
    case "message":
      return `event-${item.event.id}`;
    case "tool":
      return `event-${item.interaction.anchorId}`;
    case "activity_group":
      return `event-${item.group.anchorId}`;
  }
}

function timelineRowTop(rowId: string | null): number | null {
  if (!rowId) return null;
  const row = document.getElementById(rowId);
  return row ? row.getBoundingClientRect().top : null;
}

function ActionRow({ action }: { action: TimelineAction }) {
  const provider = action.action.provider || null;
  return (
    <div
      id={`action-${action.action.event_id ?? action.key}`}
      className="tl-system-action"
      data-testid="session-timeline-action"
      data-row-kind="action"
      aria-label={action.label}
    >
      <span className="tl-system-action__rule" />
      <span className="tl-system-action__label">{action.label}</span>
      {provider ? <span className="tl-system-action__provider">{provider}</span> : null}
      <span className="tl-system-action__time">{formatTime(action.timestamp)}</span>
    </div>
  );
}

const MESSAGE_COLLAPSE_LINE_LIMIT = 600;
const MESSAGE_PREVIEW_HEAD_LINES = 220;
const MESSAGE_PREVIEW_TAIL_LINES = 80;

function messageLineCount(text: string): number {
  return text === "" ? 0 : text.split("\n").length;
}

function mediaRefsForEvents(...events: Array<AgentEvent | null | undefined>): AgentEventMediaRef[] {
  const refs: AgentEventMediaRef[] = [];
  const seen = new Set<string>();
  for (const event of events) {
    for (const ref of event?.media_refs ?? []) {
      const key = `${ref.sha256}:${ref.thumb_url || ref.blob_url}`;
      if (seen.has(key)) continue;
      seen.add(key);
      refs.push(ref);
    }
  }
  return refs;
}

// The preview is a downscale of the original, so the original's dimensions
// give the tile its ratio before any bytes arrive and the row never reflows.
function mediaAspectRatio(ref: AgentEventMediaRef): string | undefined {
  return ref.width && ref.height ? `${ref.width} / ${ref.height}` : undefined;
}

function MediaStrip({
  mediaRefs,
  variant = "message",
}: {
  mediaRefs: AgentEventMediaRef[];
  variant?: "message" | "tool";
}) {
  // A preview that cannot load is a state the row has to show, not a broken
  // image glyph. Keyed per rendering, so a re-render retries once.
  const [failed, setFailed] = useState<ReadonlySet<string>>(() => new Set());
  if (mediaRefs.length === 0) return null;
  return (
    <div className={`tl-media tl-media--${variant}`} data-testid="session-event-media">
      {mediaRefs.map((ref) => {
        const key = `${ref.sha256}:${ref.blob_url}:${ref.thumb_url || ""}`;
        const present = ref.media_state === "present";
        const imageLike = !ref.mime_type || ref.mime_type.startsWith("image/");
        if (!present || !imageLike || failed.has(key)) {
          return (
            <span key={key} className="tl-media__placeholder">
              {ref.media_state === "pending" ? "Media pending" : "Media unavailable"}
            </span>
          );
        }
        // Only a generated preview is a still; the original GIF still animates.
        const animatedStill = ref.mime_type === "image/gif" && Boolean(ref.thumb_url);
        return (
          <a
            key={key}
            className="tl-media__item"
            href={ref.blob_url}
            target="_blank"
            rel="noreferrer noopener"
          >
            <img
              className="tl-media__image"
              src={ref.thumb_url || ref.blob_url}
              alt={`Session media ${ref.sha256.slice(0, 12)}`}
              loading="lazy"
              decoding="async"
              width={ref.width ?? undefined}
              height={ref.height ?? undefined}
              style={{ aspectRatio: mediaAspectRatio(ref) }}
              onError={() => setFailed((previous) => new Set(previous).add(key))}
            />
            {animatedStill ? <span className="tl-media__note">Animated · first frame</span> : null}
          </a>
        );
      })}
    </div>
  );
}

function hiddenLineMarker(hiddenLines: number): string {
  return `... ${hiddenLines.toLocaleString()} line${hiddenLines === 1 ? "" : "s"} hidden ...`;
}

/** Build a line-count based sandwich preview. This keeps the start and end of
 *  dump-sized messages visible, so final conclusions and error lines are not
 *  hidden behind expansion. */
function truncateMarkdown(text: string): string {
  const lines = text.split("\n");
  if (lines.length <= MESSAGE_COLLAPSE_LINE_LIMIT) return text;

  const tailCount = Math.min(MESSAGE_PREVIEW_TAIL_LINES, Math.max(0, lines.length - MESSAGE_PREVIEW_HEAD_LINES));
  let headCut = Math.min(MESSAGE_PREVIEW_HEAD_LINES, lines.length - tailCount);
  const tailStart = lines.length - tailCount;

  // Count fences (``` or ~~~) in the head; if odd, we're inside a fence.
  // Drop back to before the opening fence so the omitted marker and tail
  // remain prose instead of being rendered as part of a code block.
  const isFence = (line: string) => /^\s*(```|~~~)/.test(line);
  let fences = 0;
  for (let i = 0; i < headCut; i++) {
    if (isFence(lines[i])) fences++;
  }
  if (fences % 2 === 1) {
    for (let i = headCut - 1; i >= 0; i--) {
      if (isFence(lines[i])) {
        headCut = i;
        break;
      }
    }
  }

  const hiddenLines = Math.max(0, tailStart - headCut);
  return [
    ...lines.slice(0, headCut),
    "",
    hiddenLineMarker(hiddenLines),
    "",
    ...lines.slice(tailStart),
  ].join("\n");
}

function TurnEndRow({ turnEnd }: { turnEnd: AgentEventTurnEnd | null | undefined }) {
  if (!turnEnd) return null;
  const stopped = turnEnd.outcome === "aborted" || turnEnd.outcome === "interrupted";
  const errored = turnEnd.outcome === "error";
  const prefix = errored
    ? "Stopped with error after"
    : stopped
      ? "Interrupted after"
      : "Worked for";
  const suffix = errored ? "error" : stopped ? "stopped" : "done";
  const modifier = errored ? " tl-turn-end--error" : stopped ? " tl-turn-end--stopped" : "";
  const hookSuffix = turnEnd.stop_hook_summary?.trim() ? ` · ${turnEnd.stop_hook_summary.trim()}` : "";
  return (
    <div className={`tl-turn-end${modifier}`} data-testid="session-turn-end" data-turn-outcome={turnEnd.outcome ?? "completed"}>
      <span className="tl-turn-end__icon" aria-hidden="true">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          {errored || stopped ? (
            <path d="M3 3l6 6M9 3l-6 6" />
          ) : (
            <path d="M2.5 6.3 5 8.8l4.5-5.6" />
          )}
        </svg>
      </span>
      <span className="tl-turn-end__text">
        {prefix} {formatTurnDuration(turnEnd.duration_ms)} · {suffix} {formatTime(turnEnd.ended_at)}
        {hookSuffix}
      </span>
    </div>
  );
}

function turnEndForInteraction(interaction: ToolInteraction): AgentEventTurnEnd | null | undefined {
  return interaction.resultEvent?.turn_end ?? interaction.callEvent?.turn_end;
}

function turnEndForGroup(group: ActivityGroup): AgentEventTurnEnd | null | undefined {
  for (let i = group.interactions.length - 1; i >= 0; i -= 1) {
    const turnEnd = turnEndForInteraction(group.interactions[i]);
    if (turnEnd) return turnEnd;
  }
  return null;
}

function AttachedImages({ urls, count }: { urls: string[]; count: number }) {
  const [failed, setFailed] = useState<ReadonlySet<string>>(() => new Set());
  const live = urls.filter((url) => !failed.has(url));
  if (live.length === 0) {
    return (
      <div className="tl-msg__attached" data-testid="session-attached-images">
        {count === 1 ? "1 image attached" : `${count} images attached`}
      </div>
    );
  }
  return (
    <div className="tl-media tl-media--message" data-testid="session-attached-thumbs">
      {live.map((url) => (
        <a key={url} className="tl-media__item" href={url} target="_blank" rel="noreferrer noopener">
          <img
            className="tl-media__image"
            src={url}
            alt="Attached image"
            loading="lazy"
            decoding="async"
            onError={() => setFailed((prev) => new Set(prev).add(url))}
          />
        </a>
      ))}
    </div>
  );
}

/**
 * A transcript message. A user's send in flight renders through this same
 * component (`send` set, a stand-in event built from the outbox entry) under
 * the same React key its transcript echo will get, so the echo fills in the
 * row the user is already looking at instead of replacing it.
 */
const MessageRow = memo(function MessageRow({
  event,
  renderMedia,
  provider,
  send = null,
  viaLonghouse = false,
  apiErrorRecovered = false,
}: {
  event: Extract<TimelineItem, { kind: "message" }>["event"];
  renderMedia: boolean;
  provider: string | null;
  /** Delivery state of a send the transcript has not echoed yet. */
  send?: OutboxEntry | null;
  /** The row echoes a Longhouse send the server did not stamp with its origin. */
  viaLonghouse?: boolean;
  apiErrorRecovered?: boolean;
}) {
  const isUser = event.role === "user";
  const split = isUser ? splitAttachmentsBlock(getTimelineMessagePreview(event)) : null;
  const mediaRefs = renderMedia ? mediaRefsForEvents(event) : [];
  // A provider that records the image in its own transcript (OMP, Codex)
  // gives the row a durable media copy; Longhouse's upload link is then the
  // same image a second time, so it is drawn only when no media copy exists.
  const hasMediaCopy = mediaRefs.some((ref) => ref.media_state === "present");
  const attachmentUrls = (isUser && !hasMediaCopy && event.input_origin?.attachment_urls) || [];
  const basePreview = split && split.imageCount > 0 ? split.text.trim() : getTimelineMessagePreview(event);
  const showsImages = mediaRefs.length > 0 || attachmentUrls.length > 0;
  const preview = isUser && showsImages ? withoutImageTokenLines(basePreview) : basePreview;
  const outside = isOutsideActiveContext(event);
  const isAssistant = event.role === "assistant";
  const isLonghouseAuthored =
    event.input_origin?.authored_via === "longhouse" || viaLonghouse || Boolean(send);
  const isLong = messageLineCount(preview) > MESSAGE_COLLAPSE_LINE_LIMIT;
  const [expanded, setExpanded] = useState(false);
  const visible = isLong && !expanded
    ? truncateMarkdown(preview)
    : preview;

  const resolvedPhase = messagePhase(event) ?? undefined;
  const isCommentary = resolvedPhase === "commentary";
  const inlineApiError = event.api_error ? summarizeApiError(event.api_error, null) : null;

  return (
    <div
      id={send ? undefined : `event-${event.id}`}
      data-testid={send ? "session-outbox-row" : "session-timeline-row"}
      data-row-kind="message"
      data-message-role={event.role}
      data-outbox-state={send?.state}
      aria-busy={send?.state === "sending" || undefined}
      data-message-phase={resolvedPhase}
      className={`tl-msg tl-msg--${event.role}${send ? ` tl-msg--outbox tl-msg--outbox-${send.state}` : ""}${isCommentary ? " tl-msg--commentary" : ""}`}
    >
      {isUser ? (
        <div className="tl-msg__head tl-msg__head--ask">
          <span className="tl-msg__node" aria-hidden="true">
            <span className="tl-msg__node-ring" />
          </span>
        </div>
      ) : (
        <div className="tl-msg__head">
          {isAssistant ? (
            <span className="tl-msg__node" aria-hidden="true">
              <ProviderGlyph provider={provider} size={18} style={{ borderRadius: 999 }} />
            </span>
          ) : null}
          <span
            className="tl-msg__time"
            {...{ elementtiming: "longhouse-session-timeline-row" }}
          >
            {isAssistant ? "" : `${event.role} · `}
            {formatTime(event.timestamp)}
          </span>
          {outside ? (
            <span className="tl-chip tl-chip--warning">outside active context</span>
          ) : null}
        </div>
      )}
      {send?.warning ? (
        <div className="tl-msg__outbox-warning" role="note">
          {send.warning}
        </div>
      ) : null}
      <div className="tl-msg__body">
        {isAssistant || isUser ? (
          <MarkdownBody content={visible} />
        ) : (
          <div className="tl-msg__plain">{visible}</div>
        )}
        {isUser && !hasMediaCopy && (attachmentUrls.length > 0 || (split?.imageCount ?? 0) > 0) ? (
          <AttachedImages urls={attachmentUrls} count={Math.max(split?.imageCount ?? 0, attachmentUrls.length)} />
        ) : null}
        {send?.attachments && send.attachments.length > 0 ? (
          <div className="tl-msg__outbox-attachments" aria-label="Attachments">
            {send.attachments.map((attachment) => (
              <span key={`${attachment.filename}:${attachment.byteSize ?? ""}`}>
                {attachment.filename}
              </span>
            ))}
          </div>
        ) : null}
        <MediaStrip mediaRefs={mediaRefs} />
        {isLong ? (
          <button
            type="button"
            className="tl-msg__expand"
            aria-expanded={expanded}
            onClick={(e) => {
              e.stopPropagation();
              setExpanded((v) => !v);
            }}
          >
            {expanded ? "Collapse message" : "Show full message"}
          </button>
        ) : null}
      </div>
      {isUser ? (
        <div className="tl-msg__meta" {...{ elementtiming: "longhouse-session-timeline-row" }}>
          {send ? (
            <span className="tl-msg__outbox-status" role="status">
              {OUTBOX_STATE_LABEL[send.state]}
              {send.detail ? (
                <span className="tl-msg__outbox-detail">: {send.detail}</span>
              ) : null}
            </span>
          ) : null}
          {send?.actions?.map((action) => (
            <button
              key={action.label}
              type="button"
              className="tl-msg__outbox-action"
              onClick={action.onClick}
              disabled={action.disabled}
            >
              {action.label}
            </button>
          ))}
          <RelativeTime iso={event.timestamp} className="tl-msg__ago" testId="session-ask-time" />
          {isLonghouseAuthored ? (
            <span className="tl-msg__origin" aria-label="Sent via Longhouse" data-testid="session-input-origin-longhouse">
              via Longhouse
            </span>
          ) : null}
          {outside ? (
            <span className="tl-chip tl-chip--warning">outside active context</span>
          ) : null}
        </div>
      ) : null}
      {inlineApiError ? (
        <div
          className={`tl-api-error tl-api-error--inline${apiErrorRecovered ? " tl-api-error--recovered" : ""}`}
          data-testid="session-api-error-notice"
        >
          {inlineApiError.badge ? <span className="tl-api-error__badge">{inlineApiError.badge}</span> : null}
          <span className="tl-api-error__title">{inlineApiError.title}</span>
        </div>
      ) : null}
      <TurnEndRow turnEnd={event.turn_end} />
    </div>
  );
});

function CodeBlock({
  text,
  language,
  variant = "input",
}: {
  text: string;
  language: string | null;
  variant?: "input" | "output";
}) {
  return (
    <CodeFence
      text={text}
      language={language}
      variant={variant}
      maxHeight={variant === "output" ? 420 : 320}
      wrapLongLines
      colorizeAnsi={variant === "output"}
    />
  );
}
/** Bounded, click-free output preview of a collapsed tool call. */
function ToolOutputPreview({
  text,
  failed = false,
  pending = false,
}: {
  text: string;
  failed?: boolean;
  pending?: boolean;
}) {
  const wellRef = useRef<HTMLDivElement | null>(null);

  useLayoutEffect(() => {
    if (!pending || !wellRef.current) return;
    wellRef.current.scrollTop = wellRef.current.scrollHeight;
  }, [pending, text]);

  return (
    <div
      ref={wellRef}
      className={`tl-tool-preview${failed ? " tl-tool-preview--failure" : ""}${pending ? " tl-tool-preview--pending" : ""}`}
      data-testid={failed ? "tool-failure-preview" : "tool-output-preview"}
      role="note"
    >
      <span className="sr-only">{failed ? "Error output: " : "Tool output: "}</span>
      <pre className="tl-tool-preview__text">{text}</pre>
    </div>
  );
}

/** Inline metadata row rendered underneath an expanded tool row. */
function ToolDetail({ interaction, renderMedia }: { interaction: ToolInteraction; renderMedia: boolean }) {
  // Rows from a lite page carry previews; only those mount the body loader.
  return hasLiteBodies(interaction) ? (
    <LiteToolDetail interaction={interaction} renderMedia={renderMedia} />
  ) : (
    <ToolDetailView interaction={interaction} renderMedia={renderMedia} />
  );
}

function LiteToolDetail({ interaction, renderMedia }: { interaction: ToolInteraction; renderMedia: boolean }) {
  const full = useFullToolInteraction(interaction);
  return (
    <>
      {full.loading ? (
        <div className="tl-detail__empty" data-testid="tool-body-loading">Loading the full output…</div>
      ) : full.failed ? (
        <div className="tl-detail__empty" data-testid="tool-body-failed">
          The full output didn't load; this is the preview.
        </div>
      ) : null}
      <ToolDetailView interaction={full.interaction} renderMedia={renderMedia} />
    </>
  );
}

/** Why an unanswered call shows no output yet, from the session's evidence. */
function AwaitingResultNote({ interaction }: { interaction: ToolInteraction }) {
  const run = useRunningCallState(interaction);
  return <>{run.enabled ? run.detailNote : "No output recorded yet."}</>;
}

function ToolDetailView({
  interaction,
  renderMedia,
}: {
  interaction: ToolInteraction;
  renderMedia: boolean;
}) {
  const rawInput = interaction.callEvent?.tool_input_json;
  const presentation = interaction.presentation;
  const presentedInput = presentation?.wrapper_recedes ? presentation.tool_input_json : rawInput;
  const inputRecord = getToolInputRecord(presentedInput);
  const inputText = formatToolInput(presentedInput);
  const editStat = getEditStat(interaction);
  const hasInput = inputRecord
    ? Object.keys(inputRecord).length > 0
    : Boolean(inputText?.trim());
  const parsedOutput = interaction.resultEvent?.tool_output_text
    ? parseLonghouseOutput(interaction.resultEvent.tool_output_text)
    : null;
  const outputText = parsedOutput
    ? parsedOutput.output
    : interaction.resultEvent?.tool_output_text || null;
  const awaitingResult = !interaction.resultEvent && interaction.pairing !== "orphan";
  const dropped = isToolInteractionDropped(interaction);
  const mediaRefs = renderMedia ? mediaRefsForEvents(interaction.callEvent, interaction.resultEvent) : [];

  return (
    <div className="tl-detail">
      {presentation?.disposition === "parsed" ? (
        <section className="tl-detail__block" data-testid="tool-presentation-provenance">
          <div className="tl-detail__label">
            Parsed structure · via {presentation.execution_method || presentation.source_tool_name}
          </div>
          {(presentation.children ?? []).map((child, index) => (
            <div className="tl-detail__empty" key={`${child.rule_id}:${index}`}>
              <strong>{child.label}</strong>
              {child.tool_name !== child.label ? ` · ${child.tool_name}` : ""}
              {child.tool_input_json != null ? ` · ${(formatToolInput(child.tool_input_json) || "").replace(/\n/g, " ").slice(0, 180)}` : ""}
              {presentation.wrapper_recedes ? "" : " · result remains attributed to the enclosing wrapper"}
            </div>
          ))}
        </section>
      ) : null}
      {editStat.patch ? (
        <EditDiffView stat={editStat} />
      ) : hasInput ? (
        <section className="tl-detail__block">
          <div className="tl-detail__label">input</div>
          <CodeBlock
            text={inputText!}
            language={inputRecord ? "json" : detectLanguage(inputText!, { toolName: interaction.toolName })}
            variant="input"
          />
        </section>
      ) : null}
      <section className="tl-detail__block">
        <div className="tl-detail__label">output</div>
        {outputText ? (
          <CodeBlock
            text={outputText}
            language={detectLanguage(outputText, {
              filePath: typeof inputRecord?.file_path === "string" ? inputRecord.file_path : null,
              toolName: interaction.toolName,
            })}
            variant="output"
          />
        ) : (
          <div className="tl-detail__empty">
            {dropped
              ? "Tool call dropped — no result was ever recorded."
              : awaitingResult
                ? <AwaitingResultNote interaction={interaction} />
                : "No output recorded."}
          </div>
        )}
        <MediaStrip mediaRefs={mediaRefs} variant="tool" />
      </section>
      {presentation?.wrapper_recedes && rawInput != null ? (
        <section className="tl-detail__block">
          <div className="tl-detail__label">raw enclosing {presentation.source_tool_name}</div>
          <CodeBlock text={formatToolInput(rawInput) || ""} language="javascript" variant="input" />
        </section>
      ) : null}
    </div>
  );
}

function ActionCard({
  interaction,
  rowId,
  expanded,
  isSelected,
  onSelect,
  onToggleExpand,
  renderMedia,
  trail = false,
}: {
  interaction: ToolInteraction;
  rowId: string;
  expanded: boolean;
  isSelected: boolean;
  onSelect: () => void;
  onToggleExpand: () => void;
  renderMedia: boolean;
  trail?: boolean;
}) {
  const info = getInteractionDisplayInfo(interaction);
  const intent = getToolIntentLabel(interaction);
  // A purpose the agent wrote reads as prose and names the step on its own;
  // a target built from the arguments keeps the tool's name and mono face.
  const purpose = intent !== null && isToolIntentAuthored(interaction);
  const summary = getToolSummary(interaction);
  const exitCode = getToolExitCode(interaction);
  const duration = getToolDuration(interaction.callEvent, interaction.resultEvent);
  const dropped = isToolInteractionDropped(interaction);
  const pending = isToolInteractionRunning(interaction);
  const isAgent = isAgentToolInteraction(interaction);
  const launchesAgent = isAgent || isAgentLaunchInteraction(interaction);
  const inputRecord = getToolInputRecord(interaction.callEvent?.tool_input_json);
  const agentType = launchesAgent
    ? (inputRecord?.subagent_type as string | undefined)
    : undefined;
  const outside =
    isOutsideActiveContext(interaction.callEvent) || isOutsideActiveContext(interaction.resultEvent);

  // One predicate drives exclusion from grouping, styling, the chip, and the
  // preview. Keying styling off `exitCode` alone let structured failures render
  // as successes even though grouping had already rejected them.
  const failed = isToolInteractionFailed(interaction);
  const failurePreview = getFailurePreview(interaction);
  // A trailing call never opens its own output: the thought above is the
  // story, the output is one click away. Failures keep their preview.
  const outputPreview = failed || trail ? null : getToolOutputPreview(interaction);
  // Only edits get a diff stat: a non-edit input that happens to carry a path
  // must keep its normal summary.
  const editLabel = isEditInteraction(interaction) ? formatEditStat(getEditStat(interaction)) : null;
  const statusTone = dropped ? "error" : pending ? "pending" : failed ? "error" : "ok";
  const statusClass = pending
    ? " tl-action--pending"
    : dropped
      ? " tl-action--dropped"
      : failed
        ? " tl-action--error"
        : "";

  const detailId = `${rowId}-detail`;
  const bodyPrefetch = useToolBodyPrefetch(interaction);

  return (
    <div
      id={rowId}
      data-testid="session-timeline-row"
      data-row-kind="tool"
      data-tool-tier="action"
      data-status={statusTone}
      className={`tl-action${statusClass}${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}${isAgent ? " tl-action--agent" : ""}${trail ? " tl-trail" : ""}${purpose ? " tl-action--purpose" : ""}`}
    >
      <button
        type="button"
        className="tl-action__head"
        {...bodyPrefetch}
        onClick={() => {
          onSelect();
          onToggleExpand();
        }}
        aria-expanded={expanded}
        aria-controls={detailId}
        aria-label={purpose && intent ? `${agentType || info.displayName}: ${intent}` : undefined}
      >
        <span className="tl-action__icon" style={{ color: info.color }}>{info.icon}</span>
        {(trail && intent) || (purpose && !launchesAgent) ? null : (
          <span
            className="tl-action__name"
            {...{ elementtiming: "longhouse-session-timeline-row" }}
          >
            {agentType || info.displayName}
          </span>
        )}
        {info.mcpNamespace ? <span className="tl-action__ns">{info.mcpNamespace}</span> : null}
        <span className="tl-action__summary" title={purpose ? getToolTargetText(interaction) ?? undefined : undefined}>
          {intent ? (
            intent
          ) : editLabel ? (
            <span className="tl-action__edit-stat" data-testid="tool-edit-stat">{editLabel}</span>
          ) : (
            summary || (dropped ? "dropped" : pending ? "running…" : "")
          )}
        </span>
        <span className="tl-action__meta">
          {intent && editLabel ? (
            <span className="tl-action__edit-stat" data-testid="tool-edit-stat">{editLabel}</span>
          ) : null}
          {failed ? (
            <span className="tl-chip tl-chip--error">
              {exitCode != null && exitCode !== 0 ? `exit ${exitCode}` : "failed"}
            </span>
          ) : null}
          {pending ? <span className="tl-chip tl-chip--pending">running</span> : null}
          {dropped ? <span className="tl-chip tl-chip--warning">dropped</span> : null}
          {outside ? <span className="tl-chip tl-chip--warning">outside</span> : null}
          {duration ? <span className="tl-action__time">{duration}</span> : null}
          <span className={`tl-action__chev${expanded ? " is-open" : ""}`} aria-hidden="true">›</span>
        </span>
      </button>
      {!expanded && failurePreview ? <ToolOutputPreview text={failurePreview} failed pending={pending} /> : null}
      {!expanded && !failurePreview && outputPreview ? <ToolOutputPreview text={outputPreview} pending={pending} /> : null}
      <SubagentNode interaction={interaction} />
      {expanded ? (
        <div id={detailId}>
          <ToolDetail interaction={interaction} renderMedia={renderMedia} />
        </div>
      ) : null}
    </div>
  );
}

/**
 * Workers this tool call spawned, as a node the reader opens — not as separate
 * sessions in the timeline, and not spliced into this transcript. A 22-agent
 * fan-out inlined would bury the session it belongs to.
 */
function subagentLiveTone(child: SubagentChild): "running" | "waiting" | "done" {
  if (child.live_status === "waiting") return "waiting";
  if (child.live_status === "running" || (child.finished === false && !child.ended_at)) return "running";
  return "done";
}

function SubagentNode({ interaction }: { interaction: ToolInteraction }) {
  const children = interaction.children ?? [];
  if (children.length === 0) return null;
  const treeRows = flattenSubagentTree(children);
  return (
    <details className="tl-subagents" data-testid="tool-subagents">
      <summary className="tl-subagents__summary">
        <span className="tl-subagents__count">{summarizeSubagents(children)}</span>
      </summary>
      <ul className="tl-subagents__list">
        {treeRows.map(({ child, depth }) => {
          const tone = subagentLiveTone(child);
          const metaParts: string[] = [];
          if (child.duration_ms != null && child.duration_ms > 0) {
            metaParts.push(formatTurnDuration(child.duration_ms));
          }
          if (child.total_tokens != null && child.total_tokens > 0) {
            metaParts.push(`${formatCompactTokens(child.total_tokens)} tok`);
          }
          metaParts.push(`${child.tool_calls} ${child.tool_calls === 1 ? "call" : "calls"}`);
          return (
            <li
              key={child.session_id}
              className="tl-subagents__item"
              style={depth > 0 ? { paddingLeft: `${depth * 14}px` } : undefined}
              data-spawn-depth={depth}
            >
              <Link className="tl-subagents__link" to={`/timeline/${child.session_id}`}>
                <span className={`tl-subagents__status tl-subagents__status--${tone}`} aria-hidden="true" />
                {child.agent_type ? <span className="tl-subagents__badge">{child.agent_type}</span> : null}
                {child.is_fork ? <span className="tl-subagents__badge tl-subagents__badge--fork">fork</span> : null}
                <span className="tl-subagents__label">{subagentLabel(child)}</span>
                <span className="tl-subagents__meta">{metaParts.join(" · ")}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </details>
  );
}

function ContextLine({
  interaction,
  rowId,
  expanded,
  isSelected,
  onSelect,
  onToggleExpand,
  renderMedia,
  trail = false,
}: {
  interaction: ToolInteraction;
  rowId: string;
  expanded: boolean;
  isSelected: boolean;
  onSelect: () => void;
  onToggleExpand: () => void;
  renderMedia: boolean;
  trail?: boolean;
}) {
  const info = getInteractionDisplayInfo(interaction);
  const summary = getToolSummary(interaction);
  // An authored purpose reads as prose on a context row too (OMP writes one
  // on its reads and searches); the literal target stays on hover.
  const purpose = isToolIntentAuthored(interaction);
  const target = getToolTargetText(interaction);
  const duration = getToolDuration(interaction.callEvent, interaction.resultEvent);
  const dropped = isToolInteractionDropped(interaction);
  const pending = isToolInteractionRunning(interaction);
  const statusTone = dropped ? "error" : pending ? "pending" : "ok";
  const statusClass = pending
    ? " tl-context--pending"
    : dropped
      ? " tl-context--dropped"
      : "";

  const detailId = `${rowId}-detail`;
  const bodyPrefetch = useToolBodyPrefetch(interaction);
  return (
    <div
      id={rowId}
      data-testid="session-timeline-row"
      data-row-kind="tool"
      data-tool-tier="context"
      data-status={statusTone}
      className={`tl-context${statusClass}${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}${trail ? " tl-trail" : ""}${purpose ? " tl-context--purpose" : ""}`}
    >
      <button
        type="button"
        className="tl-context__head"
        {...bodyPrefetch}
        onClick={() => {
          onSelect();
          onToggleExpand();
        }}
        aria-expanded={expanded}
        aria-controls={detailId}
      >
        <span
          className="tl-context__label"
          style={{ color: info.color }}
          {...{ elementtiming: "longhouse-session-timeline-row" }}
        >
          {info.displayName}
        </span>
        <span className="tl-context__summary" title={target && target !== summary ? target : undefined}>
          {summary || (dropped ? "dropped" : pending ? "running…" : "")}
        </span>
        <span className="tl-context__meta">
          {pending ? <span className="tl-chip tl-chip--pending">running</span> : null}
          {dropped ? <span className="tl-chip tl-chip--warning">dropped</span> : null}
          {duration ? <span className="tl-context__time">{duration}</span> : null}
        </span>
      </button>
      {expanded ? (
        <div id={detailId}>
          <ToolDetail interaction={interaction} renderMedia={renderMedia} />
        </div>
      ) : null}
    </div>
  );
}

function ActivityChip({
  group,
  rowId,
  expanded,
  isSelected,
  expandedInteractionKeys,
  onSelect,
  onToggleExpand,
  onToggleInteraction,
  renderMedia,
  trail = false,
}: {
  group: ActivityGroup;
  rowId: string;
  expanded: boolean;
  isSelected: boolean;
  /** Full expanded set — grouped children are multi-open, like standalone rows. */
  expandedInteractionKeys: Set<string>;
  onSelect: () => void;
  onToggleExpand: () => void;
  onToggleInteraction: (key: string) => void;
  renderMedia: boolean;
  trail?: boolean;
}) {
  const [showEarlier, setShowEarlier] = useState(false);
  const summary = formatActivitySummary(group.interactions) || "Activity";
  // Trailing a thought, the line leads with what the agent said it was doing
  // (OMP `i`, Claude Bash `description`) and keeps the mechanics as the dim
  // tail. Edits are pulled onto their own lines: a file change is the work
  // product and stays legible without a click.
  const intent = trail ? firstIntentLabel(group.interactions) : null;
  const edits = trail ? group.interactions.filter(isEditInteraction) : [];
  const mechanics = trail
    ? formatActivitySummary(group.interactions.filter((interaction) => !isEditInteraction(interaction)))
    : "";
  const timing = getActivityGroupTiming(group);
  const { earlier, latest } = splitExplorationOverflow(group.interactions);
  const visibleInteractions = showEarlier ? group.interactions : latest;

  useEffect(() => {
    if (!expanded) setShowEarlier(false);
  }, [expanded]);

  useEffect(() => {
    if (expandedInteractionKeys.size === 0) return;
    if (earlier.some((interaction) => expandedInteractionKeys.has(interaction.key))) {
      setShowEarlier(true);
    }
  }, [earlier, expandedInteractionKeys]);

  return (
    <div
      id={rowId}
      data-testid="session-timeline-row"
      data-row-kind="activity-group"
      className={`tl-noise${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}${trail ? " tl-trail" : ""}`}
    >
      <button
        type="button"
        className="tl-noise__head"
        onClick={() => {
          onSelect();
          onToggleExpand();
        }}
        aria-expanded={expanded}
        aria-controls={`${rowId}-list`}
      >
        {trail ? <span className="tl-trail__glyph" aria-hidden="true">$</span> : null}
        <span
          className="tl-noise__summary"
          {...{ elementtiming: "longhouse-session-timeline-row" }}
        >
          {trail ? (intent ?? (mechanics || summary)) : summary}
        </span>
        {trail && intent && mechanics ? <span className="tl-trail__mechanics">{mechanics}</span> : null}
        <span className="tl-noise__time">{trail ? (timing.duration ?? "") : (timing.duration ?? timing.time)}</span>
        <span className={`tl-noise__chev${expanded ? " is-open" : ""}`} aria-hidden="true">›</span>
      </button>
      {!expanded && edits.length > 0 ? (
        <div className="tl-trail__edits" data-testid="trail-edits">
          {edits.map((interaction) => {
            const stat = getEditStat(interaction);
            return (
              <div key={interaction.key} className="tl-trail__edit">
                <span className="tl-trail__glyph" aria-hidden="true">✎</span>
                <span className="tl-trail__edit-path">{stat.fileName ?? getToolSummary(interaction)}</span>
                {stat.hasStat ? (
                  <span className="tl-trail__edit-stat">+{stat.added} −{stat.removed}</span>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}
      {expanded ? (
        <div className="tl-noise__list" id={`${rowId}-list`}>
          {!showEarlier && earlier.length > 0 ? (
            <button
              type="button"
              className="tl-noise__earlier"
              onClick={() => setShowEarlier(true)}
            >
              Show {earlier.length} earlier
            </button>
          ) : null}
          {visibleInteractions.map((interaction) => {
            const info = getInteractionDisplayInfo(interaction);
            const intent = getToolIntentLabel(interaction);
            const sum = getToolSummary(interaction);
            const isOpen = expandedInteractionKeys.has(interaction.key);
            // Distinct per-child IDs so focus, deep links, and keyboard nav can
            // address one diff instead of the whole group.
            const childId = `${rowId}-child-${interaction.key}`;
            const childDetailId = `${childId}-detail`;
            const editLabel = isEditInteraction(interaction)
              ? formatEditStat(getEditStat(interaction))
              : null;
            return (
              <div
                key={interaction.key}
                id={childId}
                className={`tl-noise__item${isOpen ? " is-expanded" : ""}`}
              >
                <button
                  type="button"
                  className="tl-noise__item-head"
                  onClick={() => onToggleInteraction(interaction.key)}
                  aria-expanded={isOpen}
                  aria-controls={childDetailId}
                >
                  <span className="tl-noise__item-label" style={{ color: info.color }}>
                    {info.displayName}
                  </span>
                  <span className="tl-noise__item-summary">{intent || editLabel || sum || "—"}</span>
                </button>
                {isOpen ? (
                  <div id={childDetailId}>
                    <ToolDetail interaction={interaction} renderMedia={renderMedia} />
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

// Claude Code wraps an AskUserQuestion result in fixed boilerplate. The card
// has to say what the user chose, not repeat the wrapper, so strip the two
// known sentences and keep whatever the provider put between them.
const QUESTION_ANSWER_PREFIX = "Your questions have been answered:";
const QUESTION_ANSWER_SUFFIX = "You can now continue with these answers in mind.";

function questionAnswerText(resultText: string): string {
  let text = resultText.trim();
  if (text.startsWith(QUESTION_ANSWER_PREFIX)) {
    text = text.slice(QUESTION_ANSWER_PREFIX.length).trim();
  }
  if (text.endsWith(QUESTION_ANSWER_SUFFIX)) {
    text = text.slice(0, -QUESTION_ANSWER_SUFFIX.length).trim();
  }
  return text;
}

function AskUserQuestionRow({ interaction, rowId }: { interaction: ToolInteraction; rowId: string }) {
  const rawInput = getToolInputRecord(interaction.callEvent?.tool_input_json);
  const questions = normalizeTranscriptQuestions(rawInput);
  const title = questions[0]?.header || "Question";
  const resultText = nonEmptyText(interaction.resultEvent?.tool_output_text);
  const answered = resultText != null;
  const status = answered ? "answered" : "waiting";
  const answer = resultText ? questionAnswerText(resultText) : null;

  return (
    <article
      id={rowId}
      data-testid="session-question-row"
      data-row-kind="question"
      data-status={status}
      className="tl-question"
    >
      <div className="tl-question__head">
        <span className="tl-question__eyebrow">{answered ? "Answered" : "Needs answer"}</span>
        <h3>{title}</h3>
        <p>{answered ? "Answered in the original session." : "Answer this in the terminal."}</p>
      </div>
      {answer ? (
        <div className="tl-question__answer" data-testid="session-question-answer">
          <span className="tl-question__answer-text">{answer}</span>
        </div>
      ) : null}
      {questions.length > 0 ? (
        <div className="tl-question__items">
          {questions.map((question) => (
            <section key={question.id} className="tl-question__item">
              {question.header && question.header !== title ? (
                <div className="tl-question__header">{question.header}</div>
              ) : null}
              <div className="tl-question__text">{question.question}</div>
              {question.options.length > 0 ? (
                <div className="tl-question__options" aria-label="Answer options">
                  {question.options.map((option, index) => (
                    <div
                      key={`${question.id}-${option.label}-${index}`}
                      className={`tl-question__option${resultText?.includes(option.label) ? " is-selected" : ""}`}
                      aria-disabled="true"
                    >
                      <span className="tl-question__option-label">{option.label}</span>
                      {option.description ? (
                        <span className="tl-question__option-description">{option.description}</span>
                      ) : null}
                    </div>
                  ))}
                </div>
              ) : null}
            </section>
          ))}
        </div>
      ) : (
        <div className="tl-question__text">Waiting for your answer.</div>
      )}
    </article>
  );
}

/** The first call's own words for what it was doing, with a count of the rest. */
function firstIntentLabel(interactions: ToolInteraction[]): string | null {
  const calls = interactions.filter((interaction) => !isEditInteraction(interaction));
  const first = calls.map(getToolIntentLabel).find((intent): intent is string => Boolean(intent));
  if (!first) return null;
  const rest = calls.length - 1;
  return rest > 0 ? `${first} +${rest}` : first;
}

function ToolRow(props: {
  interaction: ToolInteraction;
  rowId: string;
  expanded: boolean;
  isSelected: boolean;
  onSelect: () => void;
  onToggleExpand: () => void;
  renderMedia: boolean;
  /** Trails a thought: one dim line, no output preview unless it failed. */
  trail?: boolean;
}) {
  const { liveness } = useTimelineLiveness();
  if (liveness && isToolInteractionRunning(props.interaction) && !isAskUserQuestion(props.interaction)) {
    // Outside a narrated step a settled call shows its output in a fixed well;
    // the running call fills the same well, so the result lands in place.
    const live = props.trail ? null : getLiveToolOutputPreview(props.interaction);
    return (
      <RunningCallRow
        {...props}
        detail={<ToolDetail interaction={props.interaction} renderMedia={props.renderMedia} />}
        preview={live ? <ToolOutputPreview text={live} pending /> : null}
      />
    );
  }
  if (isAskUserQuestion(props.interaction)) {
    return <AskUserQuestionRow interaction={props.interaction} rowId={props.rowId} />;
  }
  const tier = getToolTier(props.interaction);
  if (tier === "context" || tier === "noise") {
    // A solo noise tool renders identically to a context line — one row
    // is already compact, no need for the chip/expand wrapper.
    return <ContextLine {...props} />;
  }
  return <ActionCard {...props} />;
}

const EMPTY_OUTBOX: OutboxEntry[] = [];
/** Sends whose first sight of the loaded rows is remembered. */
const FIRST_SIGHT_LIMIT = 100;

export const TimelinePane = memo(function TimelinePane({
  items,
  totalEntries,
  loadedEntries,
  abandonedEvents,
  showAbandonedBranches,
  onShowAbandonedBranchesChange,
  hasPreviousPage,
  isFetchingPreviousPage,
  onFetchPreviousPage,
  loading = false,
  error = null,
  controlOnly = false,
  selectedKey,
  onSelectKey,
  onVisibleSelectionChange,
  headerLeft,
  headerState,
  headerRight,
  headerTarget = null,
  rail = null,
  outline = null,
  turnOverlay = null,
  dock = null,
  listRef,
  renderMedia = true,
  provider = null,
  outbox = EMPTY_OUTBOX,
  liveness = null,
  topBanner = null,
  subagents = [],
}: TimelinePaneProps) {
  const [eventFilter, setEventFilter] = useState<EventFilter>("all");
  const [searchQuery, setSearchQuery] = useState("");

  // Expand state: per-tool-row and per-activity-group. Kept local so we don't
  // pollute the URL/selection with transient UI state. Selection ≠ expanded.
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  const toggleTool = useCallback((key: string) => {
    setExpandedTools((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);
  const toggleGroup = useCallback((key: string) => {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  // A deep link or external selection of a child call must reveal the exact
  // evidence instead of landing on a closed parent activity row.
  useEffect(() => {
    if (!selectedKey?.startsWith("tool:")) return;
    const interactionKey = selectedKey.slice("tool:".length);
    const parent = items.find(
      (item) => item.kind === "activity_group" && item.group.interactions.some((interaction) => interaction.key === interactionKey),
    );
    if (!parent || parent.kind !== "activity_group") return;
    setExpandedGroups((current) => current.has(parent.group.key) ? current : new Set(current).add(parent.group.key));
    setExpandedTools((current) => current.has(interactionKey) ? current : new Set(current).add(interactionKey));
  }, [items, selectedKey]);

  const topSentinelRef = useRef<HTMLDivElement | null>(null);
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const [rowsElement, setRowsElement] = useState<HTMLDivElement | null>(null);
  const paneRef = useRef<HTMLDivElement | null>(null);
  const dockRef = useRef<HTMLDivElement | null>(null);
  const hasDock = Boolean(dock);

  // The Jump to live pill sits just above the dock, which grows with the
  // composer's rows (status ledger, notices, outbox). Publish its measured
  // height so the pill clears it instead of assuming a fixed 132px.
  useLayoutEffect(() => {
    const pane = paneRef.current;
    const dockEl = dockRef.current;
    if (!pane || !dockEl) return;
    const publish = () => pane.style.setProperty("--session-dock-height", `${dockEl.offsetHeight}px`);
    publish();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(publish);
    observer.observe(dockEl);
    return () => observer.disconnect();
  }, [hasDock]);
  const attachScrollContainer = useCallback(
    (node: HTMLDivElement | null) => {
      scrollContainerRef.current = node;
      listRef?.(node);
    },
    [listRef],
  );
  useScrollToLoad({
    sentinelRef: topSentinelRef,
    rootRef: scrollContainerRef,
    enabled: hasPreviousPage,
    loading: isFetchingPreviousPage,
    onLoad: onFetchPreviousPage,
  });

  const prevScrollHeightRef = useRef(0);
  const prevItemCountRef = useRef(0);
  const prevFirstItemKeyRef = useRef<string | null>(null);
  const prevLastItemKeyRef = useRef<string | null>(null);
  const prevFirstRowIdRef = useRef<string | null>(null);
  const prevFirstRowTopRef = useRef<number | null>(null);
  // Remember whether the user was "at the bottom" *before* items or layout mutated.
  // If they were, new entries and dock/content resizes stay glued to the bottom;
  // if not, viewport stays put so the user doesn't get yanked mid-read.
  // Threshold is tight (24px) so scrolling up even a couple lines disengages
  // auto-follow immediately instead of yanking the user back down.
  const wasAtBottomRef = useRef(true);
  const lastScrollTopRef = useRef(0);
  const lastScrollHeightRef = useRef(0);
  const lastClientHeightRef = useRef(0);
  const scrollingToBottomRef = useRef(false);
  const STICK_THRESHOLD = 24;

  // Unread counter — increments while user is scrolled up and new items append.
  // Resets to 0 when the user is back at the bottom.
  const [unreadCount, setUnreadCount] = useState(0);
  const [isScrolledAway, setIsScrolledAway] = useState(false);

  const recordScrollGeometry = useCallback((container: HTMLDivElement) => {
    lastScrollTopRef.current = container.scrollTop;
    lastScrollHeightRef.current = container.scrollHeight;
    lastClientHeightRef.current = container.clientHeight;
  }, []);

  const pinToBottom = useCallback(
    (container: HTMLDivElement) => {
      scrollingToBottomRef.current = false;
      container.scrollTop = container.scrollHeight;
      wasAtBottomRef.current = true;
      recordScrollGeometry(container);
    },
    [recordScrollGeometry],
  );

  const jumpToBottomSmooth = useCallback(
    (container: HTMLDivElement) => {
      const reducedMotion =
        typeof window !== "undefined" &&
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const distance = container.scrollHeight - container.scrollTop - container.clientHeight;
      wasAtBottomRef.current = true;
      setUnreadCount(0);
      setIsScrolledAway(false);
      if (distance <= 0 || reducedMotion || typeof container.scrollTo !== "function") {
        pinToBottom(container);
        return;
      }
      scrollingToBottomRef.current = true;
      recordScrollGeometry(container);
      container.scrollTo({
        top: container.scrollHeight,
        behavior: "smooth",
      });
    },
    [pinToBottom, recordScrollGeometry],
  );

  useLayoutEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;

    const newScrollHeight = container.scrollHeight;
    const previousCount = prevItemCountRef.current;
    const previousFirstKey = prevFirstItemKeyRef.current;
    const previousLastKey = prevLastItemKeyRef.current;
    const previousFirstTop = prevFirstRowTopRef.current;
    const currentFirstKey = items.length > 0 ? timelineItemIdentity(items[0]) : null;
    const currentLastKey = items.length > 0 ? timelineItemIdentity(items[items.length - 1]) : null;
    const currentAnchorTop = timelineRowTop(prevFirstRowIdRef.current);
    let prependedCount = 0;
    let appendedCount = 0;
    let tailReplaced = false;

    // Identify which end changed from stable item identity and order. The
    // loaded entry count also rises for live appends, so it cannot identify a
    // pagination prepend by itself. One scan handles a mixed prepend+append
    // (or prepend + live-preview promotion) update without copying the list.
    if (
      previousCount > 0 &&
      items.length > previousCount &&
      previousFirstKey !== null
    ) {
      let previousFirstIndex = -1;
      let previousLastIndex = -1;
      for (let index = 0; index < items.length; index += 1) {
        const key = timelineItemIdentity(items[index]);
        if (key === previousFirstKey && previousFirstIndex < 0) previousFirstIndex = index;
        if (previousLastKey !== null && key === previousLastKey && previousLastIndex < 0) {
          previousLastIndex = index;
        }
      }
      if (previousFirstIndex >= 0) {
        prependedCount = previousFirstIndex;
        if (previousLastIndex >= previousFirstIndex) {
          appendedCount = items.length - previousLastIndex - 1;
        } else if (previousLastKey !== null) {
          tailReplaced = true;
        }
      }
    }

    if (previousCount === 0 && items.length > 0) {
      // Initial load — scroll to bottom so the most recent activity is
      // visible out of the box.
      pinToBottom(container);
    } else {
      if (prependedCount > 0) {
        const heightDelta = newScrollHeight - prevScrollHeightRef.current;
        // A mixed update also changes tail height. Anchor the first previously
        // visible row when its geometry is available so only the prepended
        // displacement is applied; pagination-only updates retain the
        // existing scrollHeight delta behavior.
        const diff =
          (appendedCount > 0 || tailReplaced) && previousFirstTop !== null && currentAnchorTop !== null
            ? currentAnchorTop - previousFirstTop
            : heightDelta;
        if (diff > 0) container.scrollTop += diff;
      }
      if (
        wasAtBottomRef.current &&
        (appendedCount > 0 || (prependedCount === 0 && newScrollHeight !== prevScrollHeightRef.current))
      ) {
        // A growing reply, tool group, or shrinking preview promotion updates
        // height at the tail without necessarily adding a row.
        pinToBottom(container);
      } else if (appendedCount > 0) {
        setUnreadCount((prev) => prev + appendedCount);
      }
    }

    recordScrollGeometry(container);
    prevScrollHeightRef.current = newScrollHeight;
    prevItemCountRef.current = items.length;
    prevFirstItemKeyRef.current = currentFirstKey;
    prevLastItemKeyRef.current = currentLastKey;
    prevFirstRowIdRef.current = items.length > 0 ? timelineItemRowId(items[0]) : null;
    prevFirstRowTopRef.current = timelineRowTop(prevFirstRowIdRef.current);
  }, [items, loadedEntries, pinToBottom, recordScrollGeometry]);

  // Keep the viewport glued to the bottom whenever the scroller shrinks (e.g.
  // dock grows when Thinking/composer/attachments appear, or window resizes)
  // or content grows (streaming deltas, image decode, outbox state changes)
  // while pinned. ResizeObserver runs after layout and before paint, so the
  // adjustment lands in the same frame with zero clipping or jump.
  useLayoutEffect(() => {
    const container = scrollContainerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;
    recordScrollGeometry(container);
    const onResize = () => {
      if (!wasAtBottomRef.current) {
        recordScrollGeometry(container);
        return;
      }
      if (scrollingToBottomRef.current) {
        // Avoid restarting smooth scroll on every streaming tick; let the
        // active scroll finish (or scrollend settle) and pin once it lands.
        recordScrollGeometry(container);
        return;
      }
      const distance = container.scrollHeight - container.scrollTop - container.clientHeight;
      if (distance > 0) {
        pinToBottom(container);
      } else {
        recordScrollGeometry(container);
      }
    };
    const observer = new ResizeObserver(onResize);
    observer.observe(container);
    if (rowsElement) observer.observe(rowsElement);
    return () => observer.disconnect();
  }, [rowsElement, pinToBottom, recordScrollGeometry]);

  // Track "at bottom" continuously so the next append or resize knows whether
  // to stick. Separate user upward scrolling from layout-induced gaps (dock
  // growth, content growth) so layout shifts never silently unpin the reader.
  useEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    recordScrollGeometry(container);
    const onScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = container;
      const distance = scrollHeight - scrollTop - clientHeight;
      const atBottom = distance < STICK_THRESHOLD;
      const scrolledUp = scrollTop < lastScrollTopRef.current - 1;
      const layoutExpandedGap =
        scrollHeight > lastScrollHeightRef.current ||
        clientHeight < lastClientHeightRef.current;

      if (scrollingToBottomRef.current) {
        if (scrolledUp) {
          scrollingToBottomRef.current = false;
          wasAtBottomRef.current = false;
        } else if (atBottom) {
          scrollingToBottomRef.current = false;
          wasAtBottomRef.current = true;
        } else {
          recordScrollGeometry(container);
          return;
        }
      } else if (wasAtBottomRef.current && !atBottom && !scrolledUp && layoutExpandedGap) {
        pinToBottom(container);
        return;
      } else {
        wasAtBottomRef.current = atBottom;
      }

      recordScrollGeometry(container);
      setIsScrolledAway(!wasAtBottomRef.current && distance > 240);
      const anchorTop = timelineRowTop(prevFirstRowIdRef.current);
      if (anchorTop !== null) prevFirstRowTopRef.current = anchorTop;
      if (wasAtBottomRef.current) {
        setUnreadCount((prev) => (prev === 0 ? prev : 0));
      }
    };
    const onScrollEnd = () => {
      if (!scrollingToBottomRef.current) return;
      scrollingToBottomRef.current = false;
      if (wasAtBottomRef.current) {
        pinToBottom(container);
      }
    };
    container.addEventListener("scroll", onScroll, { passive: true });
    container.addEventListener("scrollend", onScrollEnd, { passive: true });
    return () => {
      container.removeEventListener("scroll", onScroll);
      container.removeEventListener("scrollend", onScrollEnd);
    };
  }, [pinToBottom, recordScrollGeometry]);

  const debouncedSearch = useDebouncedValue(searchQuery, 300);
  const messageCount = useMemo(
    () => items.filter((item) => item.kind === "message").length,
    [items],
  );
  const toolCallCount = useMemo(
    () => items.reduce((count, item) => {
      if (item.kind === "tool") return count + 1;
      if (item.kind === "activity_group") return count + item.group.interactions.length;
      return count;
    }, 0),
    [items],
  );
  const outsideActiveCount = useMemo(
    () =>
      items.reduce((count, item) => {
        if (item.kind === "message" && item.event.in_active_context === false) return count + 1;
        return count;
      }, 0),
    [items],
  );

  const filteredItems = useMemo(() => {
    let result = items;

    if (eventFilter === "messages") {
      result = result.filter((item) => item.kind === "message");
    } else if (eventFilter === "tools") {
      result = result.filter((item) => item.kind === "tool" || item.kind === "activity_group");
    }

    if (!debouncedSearch.trim()) return result;

    const query = debouncedSearch.toLowerCase();
    return result.filter((item) => {
      if (item.kind === "seam") {
        return (
          item.seam.label.toLowerCase().includes(query) ||
          item.seam.description.toLowerCase().includes(query)
        );
      }
      if (item.kind === "reasoning") {
        return item.event.content_text?.toLowerCase().includes(query) ?? false;
      }
      if (item.kind === "message") {
        return item.event.content_text?.toLowerCase().includes(query);
      }
      if (item.kind === "action") {
        return (
          item.action.label.toLowerCase().includes(query) ||
          (item.action.action.provider || "").toLowerCase().includes(query)
        );
      }
      if (item.kind === "provider_notification" || item.kind === "compaction") {
        return item.event.content_text?.toLowerCase().includes(query) ?? false;
      }
      const interactions =
        item.kind === "activity_group" ? item.group.interactions : [item.interaction];
      return interactions.some((interaction) => {
        if (interaction.toolName.toLowerCase().includes(query)) return true;
        const toolInput = formatToolInput(interaction.callEvent?.tool_input_json);
        if (toolInput?.toLowerCase().includes(query)) {
          return true;
        }
        if (interaction.resultEvent?.tool_output_text?.toLowerCase().includes(query)) return true;
        return false;
      });
    });
  }, [items, eventFilter, debouncedSearch]);
  // Narrated steps (timeline-reading-experience.md, Change F): a thought leads,
  // and the tool rows that follow it until the next thought or message trail
  // under it as one dim line each. Presentation only: rows keep their own keys
  // and DOM position, so scroll anchoring and open state are untouched. A
  // thought shows the clock only when the minute changed.
  const narration = useMemo(() => {
    // The grouping is the shared model's (narrationTrail); the clock is this pane's.
    const trail = narrationTrail(filteredItems);
    // A thought carries a time only where the work paused: the first one, and
    // any that starts THOUGHT_TIME_GAP_MS after the previous thought. Steady
    // work reads as one run; a gap reads as "12 min ago".
    const thoughtTimes = new Map<string, string>();
    let lastMs: number | null = null;
    for (const item of filteredItems) {
      if (item.kind !== "reasoning") continue;
      const ms = parseUTC(item.event.timestamp).getTime();
      if (!Number.isFinite(ms)) continue;
      if (lastMs === null || ms - lastMs >= THOUGHT_TIME_GAP_MS) {
        thoughtTimes.set(timelineItemIdentity(item), item.event.timestamp);
      }
      lastMs = ms;
    }
    // A running call is current until the model speaks again after it; one
    // it has moved past is not what the work claim is about.
    const currentRunning = new Set<string>();
    for (let index = filteredItems.length - 1; index >= 0; index -= 1) {
      const item = filteredItems[index];
      if (item.kind === "reasoning" || item.kind === "message") break;
      if (item.kind === "tool" && isToolInteractionRunning(item.interaction)) {
        currentRunning.add(item.interaction.key);
      }
    }
    return { trail, thoughtTimes, currentRunning };
  }, [filteredItems]);
  const lastProgressIndex = useMemo(() => {
    for (let idx = filteredItems.length - 1; idx >= 0; idx -= 1) {
      const candidate = filteredItems[idx];
      if (candidate.kind === "tool" || candidate.kind === "activity_group" || candidate.kind === "compaction") {
        return idx;
      }
      if (candidate.kind === "message" && !candidate.event.api_error) return idx;
      if (candidate.kind === "message" && candidate.event.api_error && (candidate.event.content_text ?? "").trim()) {
        return idx;
      }
    }
    return -1;
  }, [filteredItems]);
  const subagentByTaskKey = useMemo(() => {
    const map = new Map<string, string>();
    const addChild = (child: SubagentChild) => {
      const sessionId = child.session_id?.trim();
      if (!sessionId) return;
      const providerSessionId = child.provider_session_id?.trim();
      if (providerSessionId) {
        map.set(providerSessionId, sessionId);
        const bare = providerSessionId.startsWith("agent-") ? providerSessionId.slice(6) : providerSessionId;
        map.set(bare, sessionId);
        map.set(`agent-${bare}`, sessionId);
      }
      const parentToolCallId = child.parent_tool_call_id?.trim();
      if (parentToolCallId) {
        map.set(`tool:${parentToolCallId}`, sessionId);
      }
    };
    for (const child of subagents) addChild(child);
    for (const item of items) {
      if (item.kind === "tool") {
        for (const child of item.interaction.children ?? []) addChild(child);
      } else if (item.kind === "activity_group") {
        for (const interaction of item.group.interactions) {
          for (const child of interaction.children ?? []) addChild(child);
        }
      }
    }
    return map;
  }, [subagents, items]);
  const livenessValue = useMemo(
    () => ({ liveness, provider: provider ?? null, currentRunning: narration.currentRunning }),
    [liveness, provider, narration.currentRunning],
  );
  // A lite page holds long tool calls (input and output) as their previews, so
  // search can't see what was cut; say so beside the match count rather than
  // imply full coverage.
  const searchCoversPreviewsOnly = useMemo(
    () =>
      items.some((item) =>
        item.kind === "tool"
          ? hasLiteBodies(item.interaction)
          : item.kind === "activity_group"
            ? item.group.interactions.some(hasLiteBodies)
            : false,
      ),
    [items],
  );

  const visibleSelectedKey = useMemo(() => {
    if (!selectedKey) return null;
    return filteredItems.some((item) => timelineItemContainsSelection(item, selectedKey))
      ? selectedKey
      : null;
  }, [selectedKey, filteredItems]);

  const prevVisibleKeyRef = useRef(visibleSelectedKey);
  useEffect(() => {
    if (prevVisibleKeyRef.current !== visibleSelectedKey) {
      prevVisibleKeyRef.current = visibleSelectedKey;
      onVisibleSelectionChange?.(visibleSelectedKey);
    }
  }, [visibleSelectedKey, onVisibleSelectionChange]);

  const [filtersExpanded, setFiltersExpanded] = useState(false);
  const showFilters = filtersExpanded || eventFilter !== "all" || searchQuery.trim().length > 0;

  // Describe everything loaded, not the filtered view: the count is
  // orientation for the session, and it must not shrink as the reader filters.
  const transcriptSummary = useMemo(
    () =>
      formatTranscriptSummary(countTimelineItems(items), {
        fullyLoaded: loadedEntries >= totalEntries,
      }),
    [items, loadedEntries, totalEntries],
  );

  // Pending sends belong to the unfiltered tail; a search or kind filter is
  // reading history, not watching the conversation continue.
  const visibleOutbox =
    eventFilter === "all" && !debouncedSearch.trim() ? outbox : EMPTY_OUTBOX;
  // A new send always brings the tail into view, wherever the reader was:
  // the user just acted and expects to see their message land. Filtered or
  // searched views hide the outbox, so they are left where they are.
  // Keyed on sends, not visibility: clearing a filter is not a send.
  // Settled receipts placed among the rows are history, not a send.
  const tailOutboxCount = outbox.filter((entry) => !entry.at).length;
  const prevOutboxCountRef = useRef(tailOutboxCount);
  // The typed query, not the debounced one: a send right after typing a
  // search is still a send made while reading filtered history.
  const outboxVisible =
    eventFilter === "all" && !searchQuery.trim() && tailOutboxCount > 0;
  useLayoutEffect(() => {
    const container = scrollContainerRef.current;
    const prevCount = prevOutboxCountRef.current;
    prevOutboxCountRef.current = tailOutboxCount;
    if (!container || !outboxVisible || tailOutboxCount <= prevCount) return;
    pinToBottom(container);
    setUnreadCount(0);
    setIsScrolledAway(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- a send, not a visibility change, scrolls
  }, [tailOutboxCount, pinToBottom]);

  // The live preview is the provider answering now, so a send the Runtime
  // Host already delivered came before it: render those above the preview
  // rows instead of leaving the ask under its own reply until ingest lands.
  // Sends still in flight, queued or failed stay at the tail.
  let livePreviewStart = filteredItems.length;
  while (
    livePreviewStart > 0 &&
    isLivePreviewTimelineItem(filteredItems[livePreviewStart - 1])
  ) {
    livePreviewStart -= 1;
  }
  const hasLivePreview = livePreviewStart < filteredItems.length;

  // One send, one row. A send's row and its transcript echo share the React
  // key `send:<client_request_id>`, and in the render where the loaded rows
  // show the echo the outbox row is dropped. The echo is found by the
  // request id the server stamps on it, or by the same text match the chat
  // uses for echoes the server could not link; a row matched by text keeps
  // that key for as long as this pane lives.
  const sendKeyByEventRef = useRef(new Map<string, string>());
  // The user rows loaded when each send first appeared: none of them can be
  // its echo. Keyed by send key, so it survives the pending-to-receipt handoff.
  const loadedAtFirstSightRef = useRef(new Map<string, Set<string>>());
  const echoedSendKeys = useMemo(() => {
    const sends: SendForMatch[] = outbox.flatMap((entry) =>
      entry.clientRequestId && entry.origin !== "wake" && entry.origin !== "longhouse"
        ? [
            {
              sendKey: sendKey(entry.clientRequestId),
              clientRequestId: entry.clientRequestId,
              text: entry.text,
              at: entry.at ?? entry.sentAt ?? null,
            },
          ]
        : [],
    );
    // First sight is kept after a send leaves the outbox: if it comes back
    // (its echo briefly unloaded), it must not take the rows loaded now,
    // its own echo among them, as already there. Oldest entries go first.
    const firstSight = loadedAtFirstSightRef.current;
    while (firstSight.size > FIRST_SIGHT_LIMIT) firstSight.delete(firstSight.keys().next().value!);
    if (sends.length === 0) return new Set<string>();
    // Taken from every loaded row, not the filtered ones, and not before
    // the transcript has loaded: an empty snapshot would give an older row
    // with the same words the full allowance once it shows.
    let loadedUserRows: Set<string> | null = null;
    for (const send of sends) {
      if (firstSight.has(send.sendKey) || items.length === 0) continue;
      loadedUserRows ??= new Set(
        items.flatMap((item) =>
          item.kind === "message" && item.event.role === "user" ? [String(item.event.id)] : [],
        ),
      );
      firstSight.set(send.sendKey, loadedUserRows);
    }
    const matches = matchSendsToTranscript(sends, filteredItems, {
      loadedAtFirstSight: firstSight,
      boundTo: sendKeyByEventRef.current,
    });
    for (const [key, eventId] of matches) {
      sendKeyByEventRef.current.set(String(eventId), key);
    }
    return new Set(matches.keys());
  }, [filteredItems, items, outbox]);
  // Send keys that a row stamped with its request id already holds: a row
  // bound by text never takes one of those, so no two rows share a key.
  const stampedSendKeys = useMemo(
    () =>
      new Set(
        filteredItems.flatMap((item) =>
          item.kind === "message" &&
          item.event.role === "user" &&
          item.event.is_head_branch !== false &&
          item.event.input_origin?.client_request_id
            ? [sendKey(item.event.input_origin.client_request_id)]
            : [],
        ),
      ),
    [filteredItems],
  );
  const isShownSend = (entry: OutboxEntry) =>
    !entry.clientRequestId || !echoedSendKeys.has(sendKey(entry.clientRequestId));

  // The live preview is the provider answering now, so a send it answers came
  // before it: sending, sent and unconfirmed sends render above the preview
  // rows, where the transcript echo will land. Queued sends wait for the next
  // turn and failed ones sit by the composer, so those stay at the tail.
  const tailOutbox = visibleOutbox.filter((entry) => !entry.at && isShownSend(entry));
  const answersThisTurn = (entry: OutboxEntry) =>
    entry.state === "sending" || entry.state === "sent" || entry.state === "unconfirmed";
  const outboxBeforePreview = hasLivePreview
    ? tailOutbox.filter(answersThisTurn)
    : EMPTY_OUTBOX;
  const outboxAfterPreview = hasLivePreview
    ? tailOutbox.filter((entry) => !answersThisTurn(entry))
    : tailOutbox;
  // A settled input the transcript does not show stands at the time it was
  // sent. One older than the loaded page waits for that page: placing it at
  // the top would claim it came before everything loaded.
  const durableItems = filteredItems.slice(0, livePreviewStart);
  const firstDurableMs = durableItems.length
    ? timelineItemTimeMs(durableItems[0])
    : Number.NaN;
  // They are user messages, so the messages filter keeps them too; a text
  // search reads transcript rows only.
  const placedSource =
    (eventFilter === "all" || eventFilter === "messages") && !debouncedSearch.trim()
      ? outbox
      : EMPTY_OUTBOX;
  const placedOutbox = placedSource
    .filter(isShownSend)
    .filter((entry) => {
      const at = outboxTimeMs(entry);
      if (!Number.isFinite(at)) return false;
      return !hasPreviousPage || !Number.isFinite(firstDurableMs) || at >= firstDurableMs;
    })
    .sort((a, b) => outboxTimeMs(a) - outboxTimeMs(b));

  function renderDurableRows(): ReactNode[] {
    const rows: ReactNode[] = [];
    let next = 0;
    let sawWithheldInTurn = false;
    let tier3Run: Array<{ item: TimelineItem; index: number }> = [];

    const flushTier3Run = () => {
      if (tier3Run.length === 0) return;
      if (tier3Run.length === 1) {
        const only = tier3Run[0];
        rows.push(renderTimelineItem(only.item, only.index));
      } else {
        const labels = Array.from(
          new Set(
            tier3Run
              .map(({ item }) => tier3ItemLabel(item))
              .filter((label): label is string => Boolean(label)),
          ),
        );
        const firstKey = timelineItemIdentity(tier3Run[0].item);
        const lastKey = timelineItemIdentity(tier3Run[tier3Run.length - 1].item);
        rows.push(
          <details key={`tier3:${firstKey}:${lastKey}`} className="tl-tier3-cluster" data-testid="session-tier3-cluster">
            <summary className="tl-tier3-cluster__summary">
              <span className="tl-tier3-cluster__count">{tier3Run.length} system events</span>
              {labels.length > 0 ? <span className="tl-tier3-cluster__parts">{labels.join(", ")}</span> : null}
            </summary>
            <div className="tl-tier3-cluster__items">
              {tier3Run.map(({ item, index }) => renderTimelineItem(item, index))}
            </div>
          </details>,
        );
      }
      tier3Run = [];
    };

    for (let idx = 0; idx < durableItems.length; idx += 1) {
      const item = durableItems[idx];
      const itemMs = timelineItemTimeMs(item);
      if (next < placedOutbox.length && outboxTimeMs(placedOutbox[next]) < itemMs) {
        flushTier3Run();
        while (next < placedOutbox.length && outboxTimeMs(placedOutbox[next]) < itemMs) {
          rows.push(renderOutboxEntry(placedOutbox[next++]));
        }
      }
      if (item.kind === "seam" || (item.kind === "message" && item.event.role === "user")) {
        sawWithheldInTurn = false;
      }
      if (item.kind === "reasoning" && isWithheldReasoningEvent(item.event)) {
        const itemKey = `event:${item.event.id}`;
        const isExplicitlySelected = selectedKey === itemKey;
        if (sawWithheldInTurn && !isExplicitlySelected) {
          continue;
        }
        sawWithheldInTurn = true;
      }
      if (isTier3MechanicalItem(item, idx, lastProgressIndex)) {
        tier3Run.push({ item, index: idx });
      } else {
        flushTier3Run();
        rows.push(renderTimelineItem(item, idx));
      }
    }
    flushTier3Run();
    for (const entry of placedOutbox.slice(next)) {
      rows.push(renderOutboxEntry(entry));
    }
    return rows;
  }

  // Every row in one array: a send moving from the tail into the durable
  // rows keeps its key, so React moves its DOM node instead of remounting it.
  function renderRows(): ReactNode[] {
    return [
      ...renderDurableRows(),
      ...outboxBeforePreview.map(renderOutboxEntry),
      ...filteredItems.slice(livePreviewStart).map((item, offset) => renderTimelineItem(item, livePreviewStart + offset)),
      ...outboxAfterPreview.map(renderOutboxEntry),
    ];
  }

  function renderOutboxEntry(entry: OutboxEntry): ReactNode {
    if (entry.origin === "wake" || entry.origin === "longhouse") {
      return <OutboxRow key={entry.key} entry={entry} />;
    }
    const key = entry.clientRequestId ? sendKey(entry.clientRequestId) : entry.key;
    return (
      <MessageRow
        key={key}
        event={{
          id: key,
          role: "user",
          content_text: entry.text,
          tool_name: null,
          tool_input_json: null,
          tool_output_text: null,
          tool_call_id: null,
          timestamp: entry.at ?? entry.sentAt ?? "",
          input_origin: {
            authored_via: "longhouse",
            client_request_id: entry.clientRequestId ?? null,
          },
        }}
        renderMedia={false}
        provider={provider}
        send={entry}
      />
    );
  }

  function messageRowKey(event: AgentEvent): string | number {
    if (event.role !== "user" || event.is_head_branch === false) return event.id;
    const requestId = event.input_origin?.client_request_id;
    if (requestId) return sendKey(requestId);
    const bound = sendKeyByEventRef.current.get(String(event.id));
    return bound && !stampedSendKeys.has(bound) ? bound : event.id;
  }

  const renderTimelineItem = (item: TimelineItem, itemIndex: number) => {
    const apiErrorRecovered = itemIndex < lastProgressIndex;
    if (item.kind === "seam") {
      return <SeamRow key={item.seam.key} seam={item.seam} />;
    }

    if (item.kind === "action") {
      return <ActionRow key={item.action.key} action={item.action} />;
    }
    if (item.kind === "compaction") {
      return <CompactionRow key={item.event.id} event={item.event} />;
    }
    if (item.kind === "reasoning") {
      return (
        <ReasoningRow
          key={item.event.id}
          event={item.event}
          isSelected={timelineItemContainsSelection(item, selectedKey)}
          time={narration.thoughtTimes.get(timelineItemIdentity(item)) ?? null}
        />
      );
    }

    if (item.kind === "provider_notification") {
      const subagentSessionId = item.event.task_notification
        ? resolveTaskNotificationSubagentId(item.event.task_notification, subagentByTaskKey)
        : null;
      return (
        <ProviderNoticeRow
          key={item.event.id}
          event={item.event}
          isSelected={timelineItemContainsSelection(item, selectedKey)}
          recovered={apiErrorRecovered}
          subagentSessionId={subagentSessionId}
        />
      );
    }

    if (item.kind === "message") {
      if (
        item.event.role === "user" &&
        item.event.input_origin?.origin === "wake"
      ) {
        return (
          <ProviderNoticeRow
            key={item.event.id}
            event={item.event}
            isSelected={timelineItemContainsSelection(item, selectedKey)}
            recovered={apiErrorRecovered}
          />
        );
      }

      const key = messageRowKey(item.event);
      return (
        <MessageRow
          key={key}
          event={item.event}
          renderMedia={renderMedia}
          provider={provider}
          viaLonghouse={typeof key === "string" && key.startsWith("send:")}
          apiErrorRecovered={apiErrorRecovered}
        />
      );
    }

    if (item.kind === "tool") {
      const selectionKey = `tool:${item.interaction.key}`;
      return (
        <Fragment key={item.interaction.key}>
          <ToolRow
  interaction={item.interaction}
  rowId={`event-${item.interaction.anchorId}`}
  expanded={expandedTools.has(item.interaction.key)}
  isSelected={selectedKey === selectionKey}
  onSelect={() => onSelectKey(selectionKey)}
  onToggleExpand={() => toggleTool(item.interaction.key)}
  renderMedia={renderMedia}
  trail={narration.trail.has(timelineItemIdentity(item))}
          />
          <TurnEndRow turnEnd={turnEndForInteraction(item.interaction)} />
        </Fragment>
      );
    }

    const groupKey = `group:${item.group.key}`;
    return (
      <Fragment key={item.group.key}>
        <ActivityChip
          group={item.group}
          rowId={`event-${item.group.anchorId}`}
          expanded={expandedGroups.has(item.group.key)}
          isSelected={timelineItemContainsSelection(item, selectedKey)}
          expandedInteractionKeys={expandedTools}
          onSelect={() => onSelectKey(groupKey)}
          onToggleExpand={() => toggleGroup(item.group.key)}
          onToggleInteraction={(k) => toggleTool(k)}
          renderMedia={renderMedia}
          trail={narration.trail.has(timelineItemIdentity(item))}
        />
        <TurnEndRow turnEnd={turnEndForGroup(item.group)} />
      </Fragment>
    );
  };

  const showScopedLoading = loading && filteredItems.length === 0;
  const showScopedError = !loading && !!error && filteredItems.length === 0;

  const headerBar = (
    <div
      className={`timeline-pane__header timeline-header${headerTarget ? " timeline-pane__header--app-bar" : ""}`}
      data-testid="session-timeline-header"
    >
      <div className="timeline-pane__header-main">
        {headerLeft}
        <div className="timeline-pane__title-group">
          {/* Counts now live in the header's identity sentence; this stays
              in the DOM (hidden) so pagination state is still testable. */}
          <div
            className="timeline-pane__summary sr-only"
            data-testid="session-timeline-summary"
            data-loaded-entries={loadedEntries}
            data-total-entries={totalEntries}
          >
            {transcriptSummary}
          </div>
        </div>
      </div>
      <div className="timeline-pane__header-right">
        {headerState}
        <button
          type="button"
          className={`timeline-pane__filter-toggle${showFilters ? " is-active" : ""}`}
          onClick={() => setFiltersExpanded((prev) => !prev)}
          aria-label="Toggle filters"
          title="Toggle filters and search"
        >
          <FunnelIcon width={14} height={14} />
          {eventFilter !== "all" || searchQuery.trim() ? (
            <span className="timeline-pane__filter-toggle-dot" />
          ) : null}
        </button>
        {headerRight}
      </div>
    </div>
  );

  const paneContent = (
    <div
      ref={paneRef}
      className={`timeline-pane${dock ? " timeline-pane--with-dock" : ""}`}
      data-testid="session-timeline-pane"
    >
      {headerTarget ? createPortal(headerBar, headerTarget) : headerBar}

      {showFilters ? (
        <div className="timeline-pane__header-expandable" data-testid="session-timeline-filters">
          <div className="timeline-pane__filters">
            <button
              type="button"
              className={`timeline-pane__filter${eventFilter === "all" ? " is-active" : ""}`}
              onClick={() => setEventFilter("all")}
            >
              All ({items.length})
            </button>
            <button
              type="button"
              className={`timeline-pane__filter${eventFilter === "messages" ? " is-active" : ""}`}
              onClick={() => setEventFilter("messages")}
            >
              Messages ({messageCount})
            </button>
            <button
              type="button"
              className={`timeline-pane__filter${eventFilter === "tools" ? " is-active" : ""}`}
              onClick={() => setEventFilter("tools")}
            >
              Tools ({toolCallCount})
            </button>
          </div>
          <div className="timeline-pane__header-actions">
            {debouncedSearch.trim() ? (
              <div className="timeline-pane__match-count">
                {filteredItems.length} match{filteredItems.length === 1 ? "" : "es"}
                {searchCoversPreviewsOnly ? (
                  <span data-testid="search-preview-note"> · long tool calls searched by their preview</span>
                ) : null}
              </div>
            ) : null}
            <div className="timeline-pane__search">
              <input
                type="text"
                className="timeline-pane__search-input"
                placeholder="Search messages..."
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
              />
            </div>
          </div>
        </div>
      ) : null}

      {outsideActiveCount > 0 || abandonedEvents > 0 ? (
        <div className="timeline-pane__status-row">
          {outsideActiveCount > 0 ? (
            <span className="timeline-pane__status-chip timeline-pane__status-chip--warning">
              {outsideActiveCount} outside active context
            </span>
          ) : null}
          {abandonedEvents > 0 ? (
            <button
              type="button"
              className="timeline-pane__status-chip"
              onClick={() => onShowAbandonedBranchesChange(!showAbandonedBranches)}
            >
              {showAbandonedBranches
                ? "Hide abandoned branches"
                : `${abandonedEvents} events from abandoned branches hidden · Show`}
            </button>
          ) : null}
        </div>
      ) : null}

      {topBanner}

      <div className="timeline-pane__body">
        <div
          ref={attachScrollContainer}
          className="timeline-pane__list timeline-events"
          data-testid="session-timeline-list"
        >
        {hasPreviousPage || isFetchingPreviousPage ? (
          <div
            ref={topSentinelRef}
            className="timeline-pane__load-older"
            data-testid="session-timeline-load-older"
          >
            {hasPreviousPage ? (
              <button
                type="button"
                className="timeline-pane__load-older-button"
                disabled={isFetchingPreviousPage}
                onClick={onFetchPreviousPage}
              >
                {isFetchingPreviousPage ? (
                  <>
                    <Spinner size="sm" />
                    <span>Loading earlier messages...</span>
                  </>
                ) : (
                  "Load earlier messages"
                )}
              </button>
            ) : isFetchingPreviousPage ? (
              <Spinner size="sm" />
            ) : null}
          </div>
        ) : null}
        {showScopedLoading ? (
          <EmptyState
            icon={<Spinner size="lg" />}
            title="Loading timeline..."
            description="Fetching the stitched thread timeline."
          />
        ) : showScopedError ? (
          <EmptyState
            variant="error"
            title="Couldn't load this conversation"
            description="Longhouse couldn't read this session's messages. Reload the page to try again."
            details={errorDetails(error)}
          />
        ) : filteredItems.length === 0 && visibleOutbox.length === 0 && placedOutbox.length === 0 ? (
          <EmptyState
            title="No events"
            description={
              debouncedSearch.trim()
                ? `No messages match "${debouncedSearch}".`
                : eventFilter !== "all"
                  ? "No messages match the selected filter."
                  : controlOnly
                    ? "This live session is connected for control, but its provider does not expose transcript history yet."
                    : "This session has no recorded messages."
            }
          />
        ) : (
          // A plain block wrapper around just the rows (not the sentinel or
          // empty/loading states): the trace conductor (item 4,
          // web-restyle-signal) is an absolutely positioned child of THIS
          // element rather than of the scrolling `.timeline-pane__list`
          // itself, because an absolutely positioned box sized off `top`/
          // `bottom` takes its height from its containing block's own laid
          // -out box — for a scroll container that's the visible clientHeight,
          // not the full scrollable content height, so the line only ever
          // covered the first screenful of a long transcript and vanished
          // once the view auto-scrolled to the bottom. This wrapper is a
          // normal (non-scrolling) block, so its height is the true content
          // height and the line spans the whole transcript.
          <div ref={setRowsElement} className="timeline-pane__rows">
          {renderRows()}
          </div>
        )}
        {rail ? <div className="timeline-pane__rail-anchor">{rail}</div> : null}
        </div>
        {turnOverlay}
      </div>

      {unreadCount > 0 ? (
        <button
          type="button"
          className="timeline-pane__unread-pill"
          data-testid="timeline-unread-pill"
          onClick={() => {
            const container = scrollContainerRef.current;
            if (!container) return;
            jumpToBottomSmooth(container);
          }}
        >
          ↓ {unreadCount} new
        </button>
      ) : isScrolledAway && liveness?.facts?.disposition?.state === "open" ? (
        <button
          type="button"
          className="timeline-pane__unread-pill timeline-pane__unread-pill--jump"
          data-testid="timeline-jump-live-pill"
          onClick={() => {
            const container = scrollContainerRef.current;
            if (!container) return;
            jumpToBottomSmooth(container);
          }}
        >
          ↓ Jump to live
        </button>
      ) : null}

      {dock ? <div ref={dockRef} className="timeline-pane__dock">{dock}</div> : null}
    </div>
  );

  const livePane = <TimelineLivenessProvider value={livenessValue}>{paneContent}</TimelineLivenessProvider>;

  // Keep `.timeline-pane-shell` mounted unconditionally so toggling `outline`
  // (Pin/Unpin or turn count crossing 2) never changes the root element type
  // and never remounts the transcript scroller or SessionChat composer draft.
  return (
    <div className="timeline-pane-shell" data-testid="session-timeline-shell">
      {livePane}
      {outline}
    </div>
  );
});
