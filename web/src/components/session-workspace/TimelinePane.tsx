import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { EmptyState, Spinner } from "../ui";
import { FunnelIcon } from "../icons";
import type {
  TimelineSeam,
  TimelineItem,
  ToolBatch,
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
  isAgentToolInteraction,
  isOutsideActiveContext,
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
  /** Navigation / context content rendered at the start of the header bar. */
  headerLeft?: ReactNode;
  /** Actions rendered at the far right of the header bar. */
  headerRight?: ReactNode;
  dock?: ReactNode;
  listRef?: (node: HTMLDivElement | null) => void;
}

function SeamRow({ seam }: { seam: TimelineSeam }) {
  return (
    <div className="timeline-boundary" data-testid="session-timeline-seam">
      <div className="timeline-boundary__rule" />
      <div className="timeline-boundary__body">
        <span className="timeline-boundary__label">{seam.label}</span>
        <span className="timeline-boundary__description">{seam.description}</span>
      </div>
      <div className="timeline-boundary__stamp">{formatContinuationStamp(seam.timestamp)}</div>
    </div>
  );
}

function MessageRow({
  item,
  isSelected,
  onSelect,
}: {
  item: Extract<TimelineItem, { kind: "message" }>;
  isSelected: boolean;
  onSelect: () => void;
}) {
  const preview = getTimelineMessagePreview(item.event);
  const outsideActiveContext = isOutsideActiveContext(item.event);

  return (
    <button
      type="button"
      id={`event-${item.event.id}`}
      data-testid="session-timeline-row"
      data-row-kind="message"
      data-message-role={item.event.role}
      className={`timeline-row timeline-row--message event-item${isSelected ? " is-selected event-highlight" : ""}`}
      onClick={onSelect}
    >
      <div className="timeline-row__meta">
        <span className={`timeline-row__role timeline-row__role--${item.event.role}`}>
          {item.event.role === "user" ? "You" : item.event.role === "assistant" ? "AI" : item.event.role}
        </span>
        <span className="timeline-row__time">{formatTime(item.event.timestamp)}</span>
      </div>
      <div className="timeline-row__content timeline-row__content--message">{preview}</div>
      {outsideActiveContext ? (
        <div className="timeline-row__badges">
          <span className="timeline-row__badge timeline-row__badge--warning">Outside active context</span>
        </div>
      ) : null}
    </button>
  );
}

function ToolRow({
  interaction,
  rowId,
  isSelected,
  onSelect,
}: {
  interaction: ToolInteraction;
  rowId: string;
  isSelected: boolean;
  onSelect: () => void;
}) {
  const info = getToolDisplayInfo(interaction.toolName);
  const summary = getToolSummary(interaction);
  const exitCode = getToolExitCode(interaction);
  const duration = getToolDuration(interaction.callEvent, interaction.resultEvent);
  const outsideActiveContext =
    isOutsideActiveContext(interaction.callEvent) || isOutsideActiveContext(interaction.resultEvent);
  const pending = !interaction.resultEvent && interaction.pairing !== "orphan";
  const isAgent = isAgentToolInteraction(interaction);
  const agentType = isAgent ? (interaction.callEvent?.tool_input_json as Record<string, unknown> | null)?.subagent_type as string | undefined : undefined;

  return (
    <button
      type="button"
      id={rowId}
      data-testid="session-timeline-row"
      data-row-kind="tool"
      className={`timeline-row timeline-row--tool${isAgent ? " timeline-row--agent" : ""} event-item${isSelected ? " is-selected event-highlight" : ""}`}
      onClick={onSelect}
    >
      <div className="timeline-row__meta">
        <span className="timeline-row__tool-title">
          <span className="timeline-row__tool-icon" style={{ backgroundColor: info.color }}>
            {info.icon}
          </span>
          <span className="timeline-row__tool-name">{agentType || info.displayName}</span>
          {info.mcpNamespace ? <span className="timeline-row__tool-namespace">{info.mcpNamespace}</span> : null}
        </span>
        <span className="timeline-row__time">
          {formatTime(interaction.callEvent?.timestamp ?? interaction.resultEvent?.timestamp ?? interaction.timestamp)}
        </span>
      </div>
      <div className="timeline-row__content timeline-row__content--tool">
        {summary || (pending ? "Waiting for tool result..." : "No input or output recorded")}
      </div>
      <div className="timeline-row__badges">
        {exitCode != null ? (
          <span className={`timeline-row__badge ${exitCode === 0 ? "timeline-row__badge--success" : "timeline-row__badge--error"}`}>
            exit {exitCode}
          </span>
        ) : null}
        {duration ? <span className="timeline-row__badge">{duration}</span> : null}
        {pending ? <span className="timeline-row__badge">Pending</span> : null}
        {outsideActiveContext ? (
          <span className="timeline-row__badge timeline-row__badge--warning">Outside active context</span>
        ) : null}
      </div>
    </button>
  );
}

function ToolBatchRow({
  batch,
  isSelected,
  onSelect,
}: {
  batch: ToolBatch;
  isSelected: boolean;
  onSelect: () => void;
}) {
  const allAgents = batch.interactions.every(isAgentToolInteraction);

  return (
    <button
      type="button"
      id={`event-${batch.anchorId}`}
      data-testid="session-timeline-row"
      data-row-kind="tool-batch"
      className={`timeline-row timeline-row--batch${allAgents ? " timeline-row--agent" : ""} event-item${isSelected ? " is-selected event-highlight" : ""}`}
      onClick={onSelect}
    >
      <div className="timeline-row__meta">
        <span className="timeline-row__batch-label">
          <span className={`timeline-row__badge${allAgents ? "" : " timeline-row__badge--accent"}`}>
            {batch.interactions.length} {allAgents ? "agents" : "parallel"}
          </span>
          {allAgents ? null : <span>Tool burst</span>}
        </span>
        <span className="timeline-row__time">{formatTime(batch.timestamp)}</span>
      </div>
      <div className="timeline-row__batch-list">
        {batch.interactions.map((interaction) => {
          const info = getToolDisplayInfo(interaction.toolName);
          return (
            <span key={interaction.key} className="timeline-row__batch-chip">
              <span className="timeline-row__batch-chip-icon" style={{ color: info.color }}>
                {info.icon}
              </span>
              {getToolSummary(interaction) || info.displayName}
            </span>
          );
        })}
      </div>
    </button>
  );
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
  // Filter and search state — owned here, not passed in
  const [eventFilter, setEventFilter] = useState<EventFilter>("all");
  const [searchQuery, setSearchQuery] = useState("");

  // Auto-fetch older events when the top sentinel scrolls into view.
  // The sentinel lives inside timeline-pane__list (the scroll container),
  // so we pass that div as `root` — otherwise the viewport-based observer
  // never fires when the user scrolls within the inner div.
  const topSentinelRef = useRef<HTMLDivElement | null>(null);
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  useScrollToLoad({
    sentinelRef: topSentinelRef,
    rootRef: scrollContainerRef,
    enabled: hasPreviousPage,
    loading: isFetchingPreviousPage,
    onLoad: onFetchPreviousPage,
  });

  // Scroll anchoring: when a new page prepends items, preserve the user's
  // visual position by offsetting scrollTop by the added height. This moves
  // the sentinel off-screen so the observer can fire again on the next
  // scroll-up — no "scroll down to reset" needed.
  const prevScrollHeightRef = useRef(0);
  const prevLoadedEntriesRef = useRef(0);
  useLayoutEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    const newScrollHeight = container.scrollHeight;
    const prevLoaded = prevLoadedEntriesRef.current;
    prevLoadedEntriesRef.current = loadedEntries;
    // Only anchor after the initial load (prevLoaded > 0), not on first render.
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
    () => items.filter((item) => item.kind === "tool" || item.kind === "tool_batch").length,
    [items],
  );

  const outsideActiveCount = useMemo(
    () => items.reduce((count, item) => {
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
      result = result.filter((item) => item.kind === "tool" || item.kind === "tool_batch");
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

      const interactions = item.kind === "tool_batch" ? item.batch.interactions : [item.interaction];
      return interactions.some((interaction) => {
        if (interaction.toolName.toLowerCase().includes(query)) return true;
        if (
          interaction.callEvent?.tool_input_json &&
          JSON.stringify(interaction.callEvent.tool_input_json).toLowerCase().includes(query)
        ) {
          return true;
        }
        if (interaction.resultEvent?.tool_output_text?.toLowerCase().includes(query)) {
          return true;
        }
        return false;
      });
    });
  }, [items, eventFilter, debouncedSearch]);

  // Tell the parent when the selected key becomes hidden/visible due to local filtering
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

  const toolFilterLabel = `Tools (${toolRowCount})`;
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
        {headerRight && (
          <div className="timeline-pane__header-right">
            {headerRight}
          </div>
        )}
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
              {toolFilterLabel}
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
              {showAbandonedBranches ? "Showing head + abandoned branches" : `${abandonedEvents} abandoned branch events hidden`}
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
                  item={item}
                  isSelected={timelineItemContainsSelection(item, selectedKey)}
                  onSelect={() => onSelectKey(`message:${item.event.id}`)}
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
                  isSelected={selectedKey === selectionKey}
                  onSelect={() => onSelectKey(selectionKey)}
                />
              );
            }

            return (
              <ToolBatchRow
                key={item.batch.key}
                batch={item.batch}
                isSelected={timelineItemContainsSelection(item, selectedKey)}
                onSelect={() => onSelectKey(`batch:${item.batch.key}`)}
              />
            );
          })
        )}
      </div>

      {dock ? <div className="timeline-pane__dock">{dock}</div> : null}
    </div>
  );
}
