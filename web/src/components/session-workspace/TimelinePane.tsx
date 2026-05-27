import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { EmptyState, Spinner } from "../ui";
import { FunnelIcon } from "../icons";
import type {
  NoiseGroup,
  TimelineItem,
  TimelineSeam,
  ToolInteraction,
} from "../../lib/sessionWorkspace";
import {
  formatContinuationStamp,
  formatTime,
  getTimelineMessagePreview,
  getToolDisplayInfo,
  getToolDuration,
  getToolExitCode,
  getToolSummary,
  getToolTier,
  isAgentToolInteraction,
  isOutsideActiveContext,
  isToolInteractionDropped,
  isToolInteractionRunning,
  parseLonghouseOutput,
  timelineItemContainsSelection,
} from "../../lib/sessionWorkspace";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import { useScrollToLoad } from "../../hooks/useScrollToLoad";
import { collapseUnchanged, lineDiff } from "../../lib/sessionWorkspace/diff";
import { SyntaxHighlighter, oneDark } from "../../lib/syntaxHighlighter";

type EventFilter = "all" | "messages" | "tools";

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
  selectedKey: string | null;
  onSelectKey: (key: string) => void;
  /** Called when local filtering hides/reveals the parent-selected key. */
  onVisibleSelectionChange?: (visibleKey: string | null) => void;
  /** Navigation / context content rendered at the start of the header bar. */
  headerLeft?: ReactNode;
  /** Actions rendered at the far right of the header bar. */
  headerRight?: ReactNode;
  dock?: ReactNode;
  listRef?: (node: HTMLDivElement | null) => void;
}

function SeamRow({ seam }: { seam: TimelineSeam }) {
  return (
    <div className="tl-seam" data-testid="session-timeline-seam">
      <div className="tl-seam__rule" />
      <div className="tl-seam__body">
        <span className="tl-seam__label">{seam.label}</span>
        <span className="tl-seam__description">{seam.description}</span>
      </div>
      <div className="tl-seam__stamp">{formatContinuationStamp(seam.timestamp)}</div>
    </div>
  );
}

const MESSAGE_COLLAPSE_LINE_LIMIT = 600;
const MESSAGE_PREVIEW_HEAD_LINES = 220;
const MESSAGE_PREVIEW_TAIL_LINES = 80;

function messageLineCount(text: string): number {
  return text === "" ? 0 : text.split("\n").length;
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

function MessageRow({
  event,
}: {
  event: Extract<TimelineItem, { kind: "message" }>["event"];
}) {
  const preview = getTimelineMessagePreview(event);
  const outside = isOutsideActiveContext(event);
  const isUser = event.role === "user";
  const isAssistant = event.role === "assistant";
  const isLonghouseAuthored = event.input_origin?.authored_via === "longhouse";
  const isLong = messageLineCount(preview) > MESSAGE_COLLAPSE_LINE_LIMIT;
  const [expanded, setExpanded] = useState(false);
  const visible = isLong && !expanded
    ? truncateMarkdown(preview)
    : preview;

  return (
    <div
      id={`event-${event.id}`}
      data-testid="session-timeline-row"
      data-row-kind="message"
      data-message-role={event.role}
      className={`tl-msg tl-msg--${event.role}`}
    >
      <div className="tl-msg__head">
        <span className="tl-msg__who">
          {isUser ? "You" : isAssistant ? "AI" : event.role}
        </span>
        <span className="tl-msg__time">{formatTime(event.timestamp)}</span>
        {outside ? (
          <span className="tl-chip tl-chip--warning">outside active context</span>
        ) : null}
        {isUser && isLonghouseAuthored ? (
          <span className="tl-chip" aria-label="Sent via Longhouse" data-testid="session-input-origin-longhouse">
            Longhouse
          </span>
        ) : null}
      </div>
      <div className="tl-msg__body">
        {isAssistant || isUser ? (
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              a: ({ node: _node, ...props }) => (
                <a {...props} target="_blank" rel="noreferrer noopener" />
              ),
            }}
          >
            {visible}
          </ReactMarkdown>
        ) : (
          <div className="tl-msg__plain">{visible}</div>
        )}
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
    </div>
  );
}

/** Only languages PrismLight registers in lib/syntaxHighlighter.ts.
 *  Anything else falls back to a plain <pre> so we don't trigger a silent
 *  no-highlight render. Extend both lists in lockstep. */
const EXT_TO_LANG: Record<string, string> = {
  ts: "typescript", tsx: "tsx", js: "javascript", jsx: "jsx", mjs: "javascript",
  cjs: "javascript", py: "python", sh: "bash", bash: "bash", zsh: "bash",
  fish: "bash", json: "json", jsonc: "json", yaml: "yaml", yml: "yaml",
  md: "markdown", markdown: "markdown", css: "css", sql: "sql",
};

function detectLanguage(text: string, hint: { filePath?: string | null; toolName?: string } = {}): string | null {
  const path = hint.filePath?.toLowerCase() ?? "";
  if (path) {
    const ext = path.includes(".") ? path.slice(path.lastIndexOf(".") + 1) : "";
    if (ext && EXT_TO_LANG[ext]) return EXT_TO_LANG[ext];
    if (path.endsWith("/makefile") || path.endsWith("makefile")) return "bash";
  }
  const trimmed = text.trimStart();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try { JSON.parse(trimmed); return "json"; } catch { /* not json */ }
  }
  if (hint.toolName && /bash|shell|exec/i.test(hint.toolName)) return "bash";
  return null;
}

function CodeBlock({
  text,
  language,
  variant = "input",
}: {
  text: string;
  language: string | null;
  variant?: "input" | "output";
}) {
  const maxHeight = variant === "output" ? 420 : 320;
  if (!language) {
    return <pre className={`tl-code${variant === "output" ? " tl-code--output" : ""}`}>{text}</pre>;
  }
  return (
    <SyntaxHighlighter
      language={language}
      style={oneDark}
      customStyle={{
        margin: 0,
        padding: "8px 10px",
        borderRadius: "var(--radius-md)",
        border: "1px solid rgba(158, 124, 90, 0.14)",
        fontSize: "11.5px",
        lineHeight: 1.5,
        maxHeight,
        background: "rgba(12, 10, 8, 0.78)",
      }}
      codeTagProps={{ style: { fontFamily: "var(--font-family-mono)" } }}
      wrapLongLines
    >
      {text}
    </SyntaxHighlighter>
  );
}

/** Extract old/new strings from an Edit-shape input, if present. */
function editPatchFromInput(
  input: Record<string, unknown> | null | undefined,
): { filePath: string | null; oldStr: string; newStr: string } | null {
  if (!input) return null;
  const oldStr = input.old_string;
  const newStr = input.new_string;
  if (typeof oldStr !== "string" || typeof newStr !== "string") return null;
  const filePath = typeof input.file_path === "string" ? input.file_path : null;
  return { filePath, oldStr, newStr };
}

function EditDiffView({ patch }: { patch: { filePath: string | null; oldStr: string; newStr: string } }) {
  const lines = collapseUnchanged(lineDiff(patch.oldStr, patch.newStr), 2);
  const removed = lines.filter((l) => l.kind === "remove").length;
  const added = lines.filter((l) => l.kind === "add").length;
  return (
    <section className="tl-detail__block">
      <div className="tl-detail__label">
        diff
        {patch.filePath ? <span className="tl-detail__path"> · {patch.filePath}</span> : null}
        <span className="tl-detail__diff-stat tl-detail__diff-stat--remove"> −{removed}</span>
        <span className="tl-detail__diff-stat tl-detail__diff-stat--add"> +{added}</span>
      </div>
      <div className="tl-diff">
        {lines.map((line, i) => (
          <div key={i} className={`tl-diff__line tl-diff__line--${line.kind}`}>
            <span className="tl-diff__gutter">
              {line.kind === "add" ? "+" : line.kind === "remove" ? "−" : " "}
            </span>
            <span className="tl-diff__text">{line.text || " "}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

/** Inline metadata row rendered underneath an expanded tool row. */
function ToolDetail({ interaction }: { interaction: ToolInteraction }) {
  const rawInput = interaction.callEvent?.tool_input_json as Record<string, unknown> | null | undefined;
  const editPatch = editPatchFromInput(rawInput);
  const hasInput = rawInput != null && Object.keys(rawInput).length > 0;
  const parsedOutput = interaction.resultEvent?.tool_output_text
    ? parseLonghouseOutput(interaction.resultEvent.tool_output_text)
    : null;
  const outputText = parsedOutput
    ? parsedOutput.output
    : interaction.resultEvent?.tool_output_text || null;
  const awaitingResult = !interaction.resultEvent && interaction.pairing !== "orphan";
  const dropped = isToolInteractionDropped(interaction);

  return (
    <div className="tl-detail">
      {editPatch ? (
        <EditDiffView patch={editPatch} />
      ) : hasInput ? (
        <section className="tl-detail__block">
          <div className="tl-detail__label">input</div>
          <CodeBlock text={JSON.stringify(rawInput, null, 2)} language="json" variant="input" />
        </section>
      ) : null}
      <section className="tl-detail__block">
        <div className="tl-detail__label">output</div>
        {outputText ? (
          <CodeBlock
            text={outputText}
            language={detectLanguage(outputText, {
              filePath: typeof rawInput?.file_path === "string" ? rawInput.file_path : null,
              toolName: interaction.toolName,
            })}
            variant="output"
          />
        ) : (
          <div className="tl-detail__empty">
            {dropped
              ? "Tool call dropped — no result was ever recorded."
              : awaitingResult
                ? "Result not recorded yet."
                : "No output recorded."}
          </div>
        )}
      </section>
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
}: {
  interaction: ToolInteraction;
  rowId: string;
  expanded: boolean;
  isSelected: boolean;
  onSelect: () => void;
  onToggleExpand: () => void;
}) {
  const info = getToolDisplayInfo(interaction.toolName);
  const summary = getToolSummary(interaction);
  const exitCode = getToolExitCode(interaction);
  const duration = getToolDuration(interaction.callEvent, interaction.resultEvent);
  const dropped = isToolInteractionDropped(interaction);
  const pending = isToolInteractionRunning(interaction);
  const isAgent = isAgentToolInteraction(interaction);
  const agentType = isAgent
    ? ((interaction.callEvent?.tool_input_json as Record<string, unknown> | null)?.subagent_type as string | undefined)
    : undefined;
  const outside =
    isOutsideActiveContext(interaction.callEvent) || isOutsideActiveContext(interaction.resultEvent);

  const statusTone = dropped ? "error" : pending ? "pending" : exitCode != null && exitCode !== 0 ? "error" : "ok";
  const statusClass = pending
    ? " tl-action--pending"
    : dropped
      ? " tl-action--dropped"
      : exitCode != null && exitCode !== 0
        ? " tl-action--error"
        : "";

  const detailId = `${rowId}-detail`;

  return (
    <div
      id={rowId}
      data-testid="session-timeline-row"
      data-row-kind="tool"
      data-tool-tier="action"
      data-status={statusTone}
      className={`tl-action${statusClass}${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}${isAgent ? " tl-action--agent" : ""}`}
    >
      <button
        type="button"
        className="tl-action__head"
        onClick={() => {
          onSelect();
          onToggleExpand();
        }}
        aria-expanded={expanded}
        aria-controls={detailId}
      >
        <span className="tl-action__accent" style={{ background: info.color }} data-tone={statusTone} />
        <span className="tl-action__icon" style={{ color: info.color }}>{info.icon}</span>
        <span className="tl-action__name">{agentType || info.displayName}</span>
        {info.mcpNamespace ? <span className="tl-action__ns">{info.mcpNamespace}</span> : null}
        <span className="tl-action__summary">{summary || (dropped ? "dropped" : pending ? "running…" : "")}</span>
        <span className="tl-action__meta">
          {exitCode != null && exitCode !== 0 ? <span className="tl-chip tl-chip--error">exit {exitCode}</span> : null}
          {pending ? <span className="tl-chip tl-chip--pending">running</span> : null}
          {dropped ? <span className="tl-chip tl-chip--warning">dropped</span> : null}
          {outside ? <span className="tl-chip tl-chip--warning">outside</span> : null}
          {duration ? <span className="tl-action__time">{duration}</span> : null}
          <span className={`tl-action__chev${expanded ? " is-open" : ""}`} aria-hidden="true">›</span>
        </span>
      </button>
      {expanded ? (
        <div id={detailId}>
          <ToolDetail interaction={interaction} />
        </div>
      ) : null}
    </div>
  );
}

function ContextLine({
  interaction,
  rowId,
  expanded,
  isSelected,
  onSelect,
  onToggleExpand,
}: {
  interaction: ToolInteraction;
  rowId: string;
  expanded: boolean;
  isSelected: boolean;
  onSelect: () => void;
  onToggleExpand: () => void;
}) {
  const info = getToolDisplayInfo(interaction.toolName);
  const summary = getToolSummary(interaction);
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
  return (
    <div
      id={rowId}
      data-testid="session-timeline-row"
      data-row-kind="tool"
      data-tool-tier="context"
      data-status={statusTone}
      className={`tl-context${statusClass}${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}`}
    >
      <button
        type="button"
        className="tl-context__head"
        onClick={() => {
          onSelect();
          onToggleExpand();
        }}
        aria-expanded={expanded}
        aria-controls={detailId}
      >
        <span className="tl-context__arrow">↳</span>
        <span className="tl-context__label" style={{ color: info.color }}>{info.displayName}</span>
        <span className="tl-context__summary">{summary || (dropped ? "dropped" : pending ? "running…" : "")}</span>
        <span className="tl-context__meta">
          {pending ? <span className="tl-chip tl-chip--pending">running</span> : null}
          {dropped ? <span className="tl-chip tl-chip--warning">dropped</span> : null}
          {duration ? <span className="tl-context__time">{duration}</span> : null}
        </span>
      </button>
      {expanded ? (
        <div id={detailId}>
          <ToolDetail interaction={interaction} />
        </div>
      ) : null}
    </div>
  );
}

function NoiseChip({
  group,
  rowId,
  expanded,
  isSelected,
  expandedInteractionKey,
  onSelect,
  onToggleExpand,
  onToggleInteraction,
}: {
  group: NoiseGroup;
  rowId: string;
  expanded: boolean;
  isSelected: boolean;
  expandedInteractionKey: string | null;
  onSelect: () => void;
  onToggleExpand: () => void;
  onToggleInteraction: (key: string) => void;
}) {
  const counts = new Map<string, number>();
  for (const interaction of group.interactions) {
    const info = getToolDisplayInfo(interaction.toolName);
    counts.set(info.displayName, (counts.get(info.displayName) ?? 0) + 1);
  }
  const summary = Array.from(counts.entries())
    .map(([name, n]) => (n > 1 ? `${name} × ${n}` : name))
    .join(", ");

  return (
    <div
      id={rowId}
      data-testid="session-timeline-row"
      data-row-kind="noise-group"
      className={`tl-noise${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}`}
    >
      <button
        type="button"
        className="tl-noise__head"
        onClick={() => {
          onSelect();
          onToggleExpand();
        }}
        aria-expanded={expanded}
      >
        <span className="tl-noise__arrow">↳</span>
        <span className="tl-noise__label">Explored</span>
        <span className="tl-noise__summary">{summary}</span>
        <span className="tl-noise__count">{group.interactions.length}</span>
        <span className={`tl-noise__chev${expanded ? " is-open" : ""}`} aria-hidden="true">›</span>
      </button>
      {expanded ? (
        <div className="tl-noise__list">
          {group.interactions.map((interaction) => {
            const info = getToolDisplayInfo(interaction.toolName);
            const sum = getToolSummary(interaction);
            const isOpen = expandedInteractionKey === interaction.key;
            return (
              <div
                key={interaction.key}
                className={`tl-noise__item${isOpen ? " is-expanded" : ""}`}
              >
                <button
                  type="button"
                  className="tl-noise__item-head"
                  onClick={() => onToggleInteraction(interaction.key)}
                >
                  <span className="tl-noise__item-label" style={{ color: info.color }}>
                    {info.displayName}
                  </span>
                  <span className="tl-noise__item-summary">{sum || "—"}</span>
                </button>
                {isOpen ? <ToolDetail interaction={interaction} /> : null}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function ToolRow(props: {
  interaction: ToolInteraction;
  rowId: string;
  expanded: boolean;
  isSelected: boolean;
  onSelect: () => void;
  onToggleExpand: () => void;
}) {
  const tier = getToolTier(props.interaction);
  if (tier === "context" || tier === "noise") {
    // A solo noise tool renders identically to a context line — one row
    // is already compact, no need for the chip/expand wrapper.
    return <ContextLine {...props} />;
  }
  return <ActionCard {...props} />;
}

export function TimelinePane({
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
  selectedKey,
  onSelectKey,
  onVisibleSelectionChange,
  headerLeft,
  headerRight,
  dock = null,
  listRef,
}: TimelinePaneProps) {
  const [eventFilter, setEventFilter] = useState<EventFilter>("all");
  const [searchQuery, setSearchQuery] = useState("");

  // Expand state: per-tool-row and per-noise-group. Kept local so we don't
  // pollute the URL/selection with transient UI state. Selection ≠ expanded.
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  const toggleTool = (key: string) =>
    setExpandedTools((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  const toggleGroup = (key: string) =>
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const topSentinelRef = useRef<HTMLDivElement | null>(null);
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  useScrollToLoad({
    sentinelRef: topSentinelRef,
    rootRef: scrollContainerRef,
    enabled: hasPreviousPage,
    loading: isFetchingPreviousPage,
    onLoad: onFetchPreviousPage,
  });

  const prevScrollHeightRef = useRef(0);
  const prevLoadedEntriesRef = useRef(0);
  const prevItemCountRef = useRef(0);
  // Remember whether the user was "at the bottom" *before* items mutated.
  // If they were, new entries should scroll into view; if not, viewport
  // stays put so the user doesn't get yanked mid-read.
  // Threshold is generous (80px) so a tiny manual scroll still counts as
  // "at the bottom" for auto-follow purposes.
  const wasAtBottomRef = useRef(true);
  const STICK_THRESHOLD = 80;

  useLayoutEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    const newScrollHeight = container.scrollHeight;
    const prevLoaded = prevLoadedEntriesRef.current;
    prevLoadedEntriesRef.current = loadedEntries;

    // Case 1: older entries prepended (pagination scroll preservation).
    if (prevLoaded > 0 && loadedEntries > prevLoaded) {
      const diff = newScrollHeight - prevScrollHeightRef.current;
      if (diff > 0) container.scrollTop += diff;
    }
    prevScrollHeightRef.current = newScrollHeight;
  }, [loadedEntries]);

  // Unread counter — increments while user is scrolled up and new items append.
  // Resets to 0 when the user is back at the bottom.
  const [unreadCount, setUnreadCount] = useState(0);

  // Case 2: items appended at the bottom (live events in an open session).
  // Stick to bottom only if the user was already at the bottom before the
  // append. Otherwise the viewport stays anchored to whatever they were
  // reading.
  useLayoutEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    const prevCount = prevItemCountRef.current;
    prevItemCountRef.current = items.length;
    if (prevCount === 0) {
      // Initial load — scroll to bottom so the most recent activity is
      // visible out of the box.
      container.scrollTop = container.scrollHeight;
      wasAtBottomRef.current = true;
      return;
    }
    if (items.length > prevCount) {
      if (wasAtBottomRef.current) {
        container.scrollTop = container.scrollHeight;
      } else {
        setUnreadCount((prev) => prev + (items.length - prevCount));
      }
    }
  }, [items]);

  // Track "at bottom" continuously so the next append knows whether to
  // stick. We read scrollTop on every scroll, not only on mutation, so
  // the user's intent (scrolled up = don't follow) is always current.
  useEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    const onScroll = () => {
      const distance = container.scrollHeight - container.scrollTop - container.clientHeight;
      const atBottom = distance < STICK_THRESHOLD;
      wasAtBottomRef.current = atBottom;
      if (atBottom) {
        setUnreadCount((prev) => (prev === 0 ? prev : 0));
      }
    };
    container.addEventListener("scroll", onScroll, { passive: true });
    return () => container.removeEventListener("scroll", onScroll);
  }, []);

  const debouncedSearch = useDebouncedValue(searchQuery, 300);
  const messageCount = useMemo(
    () => items.filter((item) => item.kind === "message").length,
    [items],
  );
  const toolRowCount = useMemo(
    () => items.filter((item) => item.kind === "tool" || item.kind === "noise_group").length,
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
      result = result.filter((item) => item.kind === "tool" || item.kind === "noise_group");
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
      if (item.kind === "message") {
        return item.event.content_text?.toLowerCase().includes(query);
      }
      const interactions =
        item.kind === "noise_group" ? item.group.interactions : [item.interaction];
      return interactions.some((interaction) => {
        if (interaction.toolName.toLowerCase().includes(query)) return true;
        if (
          interaction.callEvent?.tool_input_json &&
          JSON.stringify(interaction.callEvent.tool_input_json).toLowerCase().includes(query)
        ) {
          return true;
        }
        if (interaction.resultEvent?.tool_output_text?.toLowerCase().includes(query)) return true;
        return false;
      });
    });
  }, [items, eventFilter, debouncedSearch]);

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

  const showScopedLoading = loading && filteredItems.length === 0;
  const showScopedError = !loading && !!error && filteredItems.length === 0;

  return (
    <div
      className={`timeline-pane${dock ? " timeline-pane--with-dock" : ""}`}
      data-testid="session-timeline-pane"
    >
      <div className="timeline-pane__header timeline-header" data-testid="session-timeline-header">
        <div className="timeline-pane__header-main">
          {headerLeft}
          <div className="timeline-pane__title-group">
            <div className="timeline-pane__summary">
              {loadedEntries >= totalEntries
                ? `${totalEntries} entries`
                : `${loadedEntries}/${totalEntries} entries loaded`}
            </div>
          </div>
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
        </div>
        {headerRight && <div className="timeline-pane__header-right">{headerRight}</div>}
      </div>

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
              Tools ({toolRowCount})
            </button>
          </div>
          <div className="timeline-pane__header-actions">
            {debouncedSearch.trim() ? (
              <div className="timeline-pane__match-count">
                {filteredItems.length} match{filteredItems.length === 1 ? "" : "es"}
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
                ? "Showing head + abandoned branches"
                : `${abandonedEvents} abandoned branch events hidden`}
            </button>
          ) : null}
        </div>
      ) : null}

      <div
        ref={(node) => {
          scrollContainerRef.current = node;
          if (typeof listRef === "function") listRef(node);
        }}
        className="timeline-pane__list timeline-events"
        data-testid="session-timeline-list"
      >
        {hasPreviousPage || isFetchingPreviousPage ? (
          <div ref={topSentinelRef} className="timeline-pane__load-older">
            {isFetchingPreviousPage ? <Spinner size="sm" /> : null}
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
            title="Timeline unavailable"
            description={
              error instanceof Error
                ? error.message
                : "The stitched timeline failed to load for this session."
            }
          />
        ) : filteredItems.length === 0 ? (
          <EmptyState
            title="No events"
            description={
              debouncedSearch.trim()
                ? `No messages match "${debouncedSearch}".`
                : eventFilter !== "all"
                  ? "No messages match the selected filter."
                  : "This session has no recorded messages."
            }
          />
        ) : (
          filteredItems.map((item) => {
            if (item.kind === "seam") {
              return <SeamRow key={item.seam.key} seam={item.seam} />;
            }

            if (item.kind === "message") {
              return <MessageRow key={item.event.id} event={item.event} />;
            }

            if (item.kind === "tool") {
              const selectionKey = `tool:${item.interaction.key}`;
              return (
                <ToolRow
                  key={item.interaction.key}
                  interaction={item.interaction}
                  rowId={`event-${item.interaction.anchorId}`}
                  expanded={expandedTools.has(item.interaction.key)}
                  isSelected={selectedKey === selectionKey}
                  onSelect={() => onSelectKey(selectionKey)}
                  onToggleExpand={() => toggleTool(item.interaction.key)}
                />
              );
            }

            const groupKey = `group:${item.group.key}`;
            const expandedChild = Array.from(expandedTools).find((k) =>
              item.group.interactions.some((i) => i.key === k),
            );
            return (
              <NoiseChip
                key={item.group.key}
                group={item.group}
                rowId={`event-${item.group.anchorId}`}
                expanded={expandedGroups.has(item.group.key)}
                isSelected={timelineItemContainsSelection(item, selectedKey)}
                expandedInteractionKey={expandedChild ?? null}
                onSelect={() => onSelectKey(groupKey)}
                onToggleExpand={() => toggleGroup(item.group.key)}
                onToggleInteraction={(k) => toggleTool(k)}
              />
            );
          })
        )}
      </div>

      {unreadCount > 0 ? (
        <button
          type="button"
          className="timeline-pane__unread-pill"
          data-testid="timeline-unread-pill"
          onClick={() => {
            const container = scrollContainerRef.current;
            if (!container) return;
            container.scrollTo({
              top: container.scrollHeight,
              behavior: "smooth",
            });
            wasAtBottomRef.current = true;
            setUnreadCount(0);
          }}
        >
          ↓ {unreadCount} new
        </button>
      ) : null}

      {dock ? <div className="timeline-pane__dock">{dock}</div> : null}
    </div>
  );
}
