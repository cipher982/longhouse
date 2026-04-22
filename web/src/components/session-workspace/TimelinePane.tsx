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
  parseLonghouseOutput,
  timelineItemContainsSelection,
} from "../../lib/sessionWorkspace";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import { useScrollToLoad } from "../../hooks/useScrollToLoad";

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
  sessionEnded?: boolean;
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

function MessageRow({
  event,
  onRawClick,
}: {
  event: Extract<TimelineItem, { kind: "message" }>["event"];
  onRawClick: () => void;
}) {
  const preview = getTimelineMessagePreview(event);
  const outside = isOutsideActiveContext(event);
  const isUser = event.role === "user";
  const isAssistant = event.role === "assistant";

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
        <button
          type="button"
          className="tl-raw-btn"
          onClick={onRawClick}
          title="Inspect raw event"
          aria-label="Inspect raw event"
        >
          {"{}"}
        </button>
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
            {preview}
          </ReactMarkdown>
        ) : (
          <div className="tl-msg__plain">{preview}</div>
        )}
      </div>
    </div>
  );
}

/** Inline metadata row rendered underneath an expanded tool row. */
function ToolDetail({ interaction, sessionEnded }: { interaction: ToolInteraction; sessionEnded: boolean }) {
  const hasInput =
    interaction.callEvent?.tool_input_json != null &&
    Object.keys(interaction.callEvent.tool_input_json).length > 0;
  const parsedOutput = interaction.resultEvent?.tool_output_text
    ? parseLonghouseOutput(interaction.resultEvent.tool_output_text)
    : null;
  const outputText = parsedOutput
    ? parsedOutput.output
    : interaction.resultEvent?.tool_output_text || null;
  const awaitingResult = !interaction.resultEvent && interaction.pairing !== "orphan";
  const dropped = isToolInteractionDropped(interaction, sessionEnded);

  return (
    <div className="tl-detail">
      {hasInput ? (
        <section className="tl-detail__block">
          <div className="tl-detail__label">input</div>
          <pre className="tl-code">
            {JSON.stringify(interaction.callEvent?.tool_input_json, null, 2)}
          </pre>
        </section>
      ) : null}
      <section className="tl-detail__block">
        <div className="tl-detail__label">output</div>
        {outputText ? (
          <pre className="tl-code tl-code--output">{outputText}</pre>
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
  sessionEnded,
  onSelect,
  onToggleExpand,
}: {
  interaction: ToolInteraction;
  rowId: string;
  expanded: boolean;
  isSelected: boolean;
  sessionEnded: boolean;
  onSelect: () => void;
  onToggleExpand: () => void;
}) {
  const info = getToolDisplayInfo(interaction.toolName);
  const summary = getToolSummary(interaction);
  const exitCode = getToolExitCode(interaction);
  const duration = getToolDuration(interaction.callEvent, interaction.resultEvent);
  const awaitingResult = !interaction.resultEvent && interaction.pairing !== "orphan";
  const dropped = awaitingResult && isToolInteractionDropped(interaction, sessionEnded);
  const pending = awaitingResult && !dropped;
  const isAgent = isAgentToolInteraction(interaction);
  const agentType = isAgent
    ? ((interaction.callEvent?.tool_input_json as Record<string, unknown> | null)?.subagent_type as string | undefined)
    : undefined;
  const outside =
    isOutsideActiveContext(interaction.callEvent) || isOutsideActiveContext(interaction.resultEvent);

  const statusTone = dropped ? "error" : pending ? "pending" : exitCode != null && exitCode !== 0 ? "error" : "ok";

  return (
    <div
      id={rowId}
      data-testid="session-timeline-row"
      data-row-kind="tool"
      data-tool-tier="action"
      className={`tl-action${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}${isAgent ? " tl-action--agent" : ""}`}
    >
      <button
        type="button"
        className="tl-action__head"
        onClick={() => {
          onSelect();
          onToggleExpand();
        }}
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
      {expanded ? <ToolDetail interaction={interaction} sessionEnded={sessionEnded} /> : null}
    </div>
  );
}

function ContextLine({
  interaction,
  rowId,
  expanded,
  isSelected,
  sessionEnded,
  onSelect,
  onToggleExpand,
}: {
  interaction: ToolInteraction;
  rowId: string;
  expanded: boolean;
  isSelected: boolean;
  sessionEnded: boolean;
  onSelect: () => void;
  onToggleExpand: () => void;
}) {
  const info = getToolDisplayInfo(interaction.toolName);
  const summary = getToolSummary(interaction);
  const duration = getToolDuration(interaction.callEvent, interaction.resultEvent);
  const awaitingResult = !interaction.resultEvent && interaction.pairing !== "orphan";
  const dropped = awaitingResult && isToolInteractionDropped(interaction, sessionEnded);
  const pending = awaitingResult && !dropped;

  return (
    <div
      id={rowId}
      data-testid="session-timeline-row"
      data-row-kind="tool"
      data-tool-tier="context"
      className={`tl-context${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}`}
    >
      <button
        type="button"
        className="tl-context__head"
        onClick={() => {
          onSelect();
          onToggleExpand();
        }}
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
      {expanded ? <ToolDetail interaction={interaction} sessionEnded={sessionEnded} /> : null}
    </div>
  );
}

function NoiseChip({
  group,
  rowId,
  expanded,
  isSelected,
  sessionEnded,
  expandedInteractionKey,
  onSelect,
  onToggleExpand,
  onToggleInteraction,
}: {
  group: NoiseGroup;
  rowId: string;
  expanded: boolean;
  isSelected: boolean;
  sessionEnded: boolean;
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
                {isOpen ? <ToolDetail interaction={interaction} sessionEnded={sessionEnded} /> : null}
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
  sessionEnded: boolean;
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
  sessionEnded = false,
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
  useLayoutEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    const newScrollHeight = container.scrollHeight;
    const prevLoaded = prevLoadedEntriesRef.current;
    prevLoadedEntriesRef.current = loadedEntries;
    if (prevLoaded > 0 && loadedEntries > prevLoaded) {
      const diff = newScrollHeight - prevScrollHeightRef.current;
      if (diff > 0) container.scrollTop += diff;
    }
    prevScrollHeightRef.current = newScrollHeight;
  }, [loadedEntries]);

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
              return (
                <MessageRow
                  key={item.event.id}
                  event={item.event}
                  onRawClick={() => onSelectKey(`message:${item.event.id}`)}
                />
              );
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
                  sessionEnded={sessionEnded}
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
                sessionEnded={sessionEnded}
                expandedInteractionKey={expandedChild ?? null}
                onSelect={() => onSelectKey(groupKey)}
                onToggleExpand={() => toggleGroup(item.group.key)}
                onToggleInteraction={(k) => toggleTool(k)}
              />
            );
          })
        )}
      </div>

      {dock ? <div className="timeline-pane__dock">{dock}</div> : null}
    </div>
  );
}
