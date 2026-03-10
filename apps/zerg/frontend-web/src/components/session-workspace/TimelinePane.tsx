import { Button, EmptyState, Spinner } from "../ui";
import type { TimelineItem, ToolBatch, ToolInteraction, EventFilter } from "../../lib/sessionWorkspace";
import {
  formatTime,
  getPreferredSelectionKey,
  getTimelineMessagePreview,
  getToolDisplayInfo,
  getToolDuration,
  getToolExitCode,
  getToolSummary,
  isOutsideActiveContext,
  timelineItemContainsSelection,
} from "../../lib/sessionWorkspace";

interface TimelinePaneProps {
  items: TimelineItem[];
  filteredItems: TimelineItem[];
  totalEvents: number;
  loadedEvents: number;
  eventFilter: EventFilter;
  onEventFilterChange: (filter: EventFilter) => void;
  searchQuery: string;
  onSearchQueryChange: (value: string) => void;
  debouncedSearch: string;
  messageCount: number;
  toolRowCount: number;
  outsideActiveCount: number;
  abandonedEvents: number;
  showAbandonedBranches: boolean;
  onShowAbandonedBranchesChange: (show: boolean) => void;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  onFetchNextPage: () => void;
  loading?: boolean;
  error?: unknown;
  selectedKey: string | null;
  onSelectKey: (key: string) => void;
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

  return (
    <button
      type="button"
      id={rowId}
      data-testid="session-timeline-row"
      data-row-kind="tool"
      className={`timeline-row timeline-row--tool event-item${isSelected ? " is-selected event-highlight" : ""}`}
      onClick={onSelect}
    >
      <div className="timeline-row__meta">
        <span className="timeline-row__tool-title">
          <span className="timeline-row__tool-icon" style={{ backgroundColor: info.color }}>
            {info.icon}
          </span>
          <span className="timeline-row__tool-name">{info.displayName}</span>
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
  return (
    <button
      type="button"
      id={`event-${batch.anchorId}`}
      data-testid="session-timeline-row"
      data-row-kind="tool-batch"
      className={`timeline-row timeline-row--batch event-item${isSelected ? " is-selected event-highlight" : ""}`}
      onClick={onSelect}
    >
      <div className="timeline-row__meta">
        <span className="timeline-row__batch-label">
          <span className="timeline-row__badge timeline-row__badge--accent">
            {batch.interactions.length} parallel
          </span>
          <span>Tool burst</span>
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
  filteredItems,
  totalEvents,
  loadedEvents,
  eventFilter,
  onEventFilterChange,
  searchQuery,
  onSearchQueryChange,
  debouncedSearch,
  messageCount,
  toolRowCount,
  outsideActiveCount,
  abandonedEvents,
  showAbandonedBranches,
  onShowAbandonedBranchesChange,
  hasNextPage,
  isFetchingNextPage,
  onFetchNextPage,
  loading = false,
  error = null,
  selectedKey,
  onSelectKey,
}: TimelinePaneProps) {
  const toolFilterLabel = `Tools (${toolRowCount})`;
  const showScopedLoading = loading && filteredItems.length === 0;
  const showScopedError = !loading && !!error && filteredItems.length === 0;

  return (
    <div className="timeline-pane" data-testid="session-timeline-pane">
      <div className="timeline-pane__header timeline-header" data-testid="session-timeline-header">
        <div>
          <div className="timeline-pane__title">Event Timeline</div>
          <div className="timeline-pane__subtitle">
            {loadedEvents >= totalEvents
              ? `${totalEvents} events`
              : `${loadedEvents}/${totalEvents} events loaded`}
          </div>
        </div>
        <div className="timeline-pane__search">
          <input
            type="text"
            className="timeline-pane__search-input"
            placeholder="Search events..."
            value={searchQuery}
            onChange={(event) => onSearchQueryChange(event.target.value)}
          />
        </div>
      </div>

      <div className="timeline-pane__toolbar">
        <div className="timeline-pane__filters">
          <button
            type="button"
            className={`timeline-pane__filter${eventFilter === "all" ? " is-active" : ""}`}
            onClick={() => onEventFilterChange("all")}
          >
            All ({items.length})
          </button>
          <button
            type="button"
            className={`timeline-pane__filter${eventFilter === "messages" ? " is-active" : ""}`}
            onClick={() => onEventFilterChange("messages")}
          >
            Messages ({messageCount})
          </button>
          <button
            type="button"
            className={`timeline-pane__filter${eventFilter === "tools" ? " is-active" : ""}`}
            onClick={() => onEventFilterChange("tools")}
          >
            {toolFilterLabel}
          </button>
        </div>
        {debouncedSearch.trim() ? (
          <div className="timeline-pane__match-count">
            {filteredItems.length} match{filteredItems.length === 1 ? "" : "es"}
          </div>
        ) : null}
      </div>

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

      <div className="timeline-pane__list timeline-events" data-testid="session-timeline-list">
        {showScopedLoading ? (
          <EmptyState
            icon={<Spinner size="lg" />}
            title="Loading timeline..."
            description="Fetching events for this session."
          />
        ) : showScopedError ? (
          <EmptyState
            variant="error"
            title="Timeline unavailable"
            description={
              error instanceof Error
                ? error.message
                : "Events failed to load for this session."
            }
          />
        ) : filteredItems.length === 0 ? (
          <EmptyState
            title="No events"
            description={
              debouncedSearch.trim()
                ? `No events match "${debouncedSearch}".`
                : eventFilter !== "all"
                  ? "No events match the selected filter."
                  : "This session has no recorded events."
            }
          />
        ) : (
          filteredItems.map((item) => {
            if (item.kind === "message") {
              return (
                <MessageRow
                  key={item.event.id}
                  item={item}
                  isSelected={timelineItemContainsSelection(item, selectedKey)}
                  onSelect={() => onSelectKey(getPreferredSelectionKey(item))}
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

      {hasNextPage ? (
        <div className="timeline-pane__footer">
          <Button variant="ghost" size="sm" onClick={onFetchNextPage} disabled={isFetchingNextPage}>
            {isFetchingNextPage ? "Loading more..." : "Load older events"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
