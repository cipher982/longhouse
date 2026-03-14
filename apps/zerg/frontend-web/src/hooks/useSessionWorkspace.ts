import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAgentSession, useAgentSessionEventsInfinite, useAgentSessionThread } from "./useAgentSessions";
import {
  buildTimelineModel,
  getPreferredSelectionKey,
  timelineItemContainsSelection,
  type EventFilter,
} from "../lib/sessionWorkspace";

const EVENTS_PAGE_SIZE = 1000;
const AUTO_SCROLL_MAX_ATTEMPTS = 12;
const AUTO_SCROLL_EPSILON_PX = 1;

interface UseSessionWorkspaceOptions {
  highlightEventId?: number | null;
}

export function useSessionWorkspace(
  sessionId: string | null,
  options: UseSessionWorkspaceOptions = {},
) {
  const highlightEventId = options.highlightEventId ?? null;

  const { data: session, isLoading: sessionLoading, error: sessionError } = useAgentSession(sessionId);
  const { data: threadData } = useAgentSessionThread(sessionId);

  const [showAbandonedBranches, setShowAbandonedBranches] = useState(false);
  const {
    data: eventsPagesData,
    isLoading: eventsLoading,
    error: eventsError,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useAgentSessionEventsInfinite(sessionId, {
    limit: EVENTS_PAGE_SIZE,
    branch_mode: showAbandonedBranches ? "all" : "head",
  });

  const [eventFilter, setEventFilter] = useState<EventFilter>("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [timelineListElement, setTimelineListElement] = useState<HTMLDivElement | null>(null);
  const highlightedEventRef = useRef<number | null>(null);
  const autoScrolledSelectionRef = useRef(false);

  const registerTimelineList = useCallback((node: HTMLDivElement | null) => {
    setTimelineListElement((current) => (current === node ? current : node));
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(searchQuery), 300);
    return () => window.clearTimeout(timer);
  }, [searchQuery]);

  useEffect(() => {
    setShowAbandonedBranches(false);
    setEventFilter("all");
    setSearchQuery("");
    setDebouncedSearch("");
    setSelectedKey(null);
    highlightedEventRef.current = null;
    autoScrolledSelectionRef.current = false;
  }, [sessionId]);

  const events = useMemo(
    () => eventsPagesData?.pages.flatMap((page) => page.events) || [],
    [eventsPagesData],
  );

  const totalEvents = useMemo(
    () => eventsPagesData?.pages[0]?.total ?? events.length,
    [eventsPagesData, events.length],
  );

  const abandonedEvents = useMemo(
    () => eventsPagesData?.pages[0]?.abandoned_events ?? 0,
    [eventsPagesData],
  );

  const model = useMemo(() => buildTimelineModel(events), [events]);

  const threadSessions = useMemo(
    () => threadData?.sessions || (session ? [session] : []),
    [threadData, session],
  );

  const headSessionId = threadData?.head_session_id || session?.thread_head_session_id || session?.id || null;

  const currentThreadSession = useMemo(
    () => threadSessions.find((item) => item.id === session?.id) || session || null,
    [threadSessions, session],
  );

  const headThreadSession = useMemo(
    () => threadSessions.find((item) => item.id === headSessionId) || currentThreadSession,
    [threadSessions, headSessionId, currentThreadSession],
  );

  const isViewingHead =
    !!currentThreadSession &&
    !!headThreadSession &&
    currentThreadSession.id === headThreadSession.id;

  const filteredItems = useMemo(() => {
    let result = model.items;

    if (eventFilter === "messages") {
      result = result.filter((item) => item.kind === "message");
    } else if (eventFilter === "tools") {
      result = result.filter((item) => item.kind === "tool" || item.kind === "tool_batch");
    }

    if (!debouncedSearch.trim()) return result;

    const query = debouncedSearch.toLowerCase();
    return result.filter((item) => {
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
  }, [model.items, eventFilter, debouncedSearch]);

  const messageCount = useMemo(
    () => model.items.filter((item) => item.kind === "message").length,
    [model.items],
  );

  const toolRowCount = useMemo(
    () => model.items.filter((item) => item.kind === "tool" || item.kind === "tool_batch").length,
    [model.items],
  );

  const outsideActiveCount = useMemo(
    () => events.filter((event) => event.in_active_context === false).length,
    [events],
  );

  const hasHighlightEvent = useMemo(() => {
    if (highlightEventId == null) return true;
    return events.some((event) => event.id === highlightEventId);
  }, [highlightEventId, events]);

  useEffect(() => {
    if (highlightEventId == null) return;
    if (hasHighlightEvent) return;
    if (!hasNextPage || isFetchingNextPage) return;
    void fetchNextPage();
  }, [highlightEventId, hasHighlightEvent, hasNextPage, isFetchingNextPage, fetchNextPage]);

  useEffect(() => {
    if (filteredItems.length === 0) {
      setSelectedKey(null);
      return;
    }

    if (selectedKey == null) return;

    const selectionIsVisible = filteredItems.some((item) => timelineItemContainsSelection(item, selectedKey));
    if (selectionIsVisible) return;

    setSelectedKey(null);
  }, [filteredItems, selectedKey]);

  useEffect(() => {
    if (highlightEventId == null) return;
    if (!hasHighlightEvent) return;
    if (highlightedEventRef.current === highlightEventId) return;

    const selectionKey = model.eventIdToSelectionKey.get(highlightEventId);
    const rowId = model.eventIdToRowId.get(highlightEventId);

    if (selectionKey) {
      setSelectedKey(selectionKey);
    }

    let frameId: number | null = null;

    if (rowId) {
      const scrollToRow = () => {
        const target = document.getElementById(rowId);
        target?.scrollIntoView({ behavior: "smooth", block: "center" });
      };

      if (document.getElementById(rowId)) {
        scrollToRow();
      } else {
        frameId = window.requestAnimationFrame(scrollToRow);
      }
    }

    highlightedEventRef.current = highlightEventId;
    return () => {
      if (frameId != null) {
        window.cancelAnimationFrame(frameId);
      }
    };
  }, [highlightEventId, hasHighlightEvent, model.eventIdToRowId, model.eventIdToSelectionKey]);

  useEffect(() => {
    if (highlightEventId != null) return;
    if (eventsLoading) return;
    if (autoScrolledSelectionRef.current) return;
    if (filteredItems.length === 0) return;
    const targetKey =
      selectedKey || (filteredItems.length > 0 ? getPreferredSelectionKey(filteredItems[filteredItems.length - 1]) : null);
    if (!targetKey) return;

    const selection = model.selectionMap.get(targetKey);
    if (!selection) return;

    let frameId: number | null = null;
    let attempts = 0;

    const tryScrollToSelection = () => {
      attempts += 1;

      if (!selectedKey) {
        const list = timelineListElement;
        if (list instanceof HTMLElement) {
          const maxScrollTop = Math.max(0, list.scrollHeight - list.clientHeight);
          if (maxScrollTop > AUTO_SCROLL_EPSILON_PX) {
            list.scrollTop = maxScrollTop;
            if (list.scrollTop > AUTO_SCROLL_EPSILON_PX) {
              autoScrolledSelectionRef.current = true;
              return;
            }
          }

          if (attempts >= AUTO_SCROLL_MAX_ATTEMPTS) {
            autoScrolledSelectionRef.current = maxScrollTop <= AUTO_SCROLL_EPSILON_PX;
            return;
          }
        }
      } else {
        const target = document.getElementById(selection.rowId);
        if (target) {
          target.scrollIntoView({ behavior: "auto", block: "center" });
          autoScrolledSelectionRef.current = true;
          return;
        }

        if (attempts >= AUTO_SCROLL_MAX_ATTEMPTS) {
          return;
        }
      }

      frameId = window.requestAnimationFrame(tryScrollToSelection);
    };

    tryScrollToSelection();

    return () => {
      if (frameId != null) {
        window.cancelAnimationFrame(frameId);
      }
    };
  }, [highlightEventId, eventsLoading, selectedKey, filteredItems, model.selectionMap, timelineListElement]);

  const selectedSelection = useMemo(
    () => (selectedKey ? model.selectionMap.get(selectedKey) ?? null : null),
    [selectedKey, model.selectionMap],
  );

  const selectKey = (key: string) => {
    setSelectedKey(key);
  };

  return {
    session,
    sessionLoading,
    sessionError,
    threadSessions,
    headSessionId,
    currentThreadSession,
    headThreadSession,
    isViewingHead,
    showAbandonedBranches,
    setShowAbandonedBranches,
    events,
    totalEvents,
    abandonedEvents,
    eventsLoading,
    eventsError,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    items: model.items,
    toolItems: model.toolItems,
    toolBatches: model.toolBatches,
    filteredItems,
    eventFilter,
    setEventFilter,
    searchQuery,
    setSearchQuery,
    debouncedSearch,
    messageCount,
    toolRowCount,
    outsideActiveCount,
    selectedKey,
    selectedSelection,
    selectKey,
    registerTimelineList,
  };
}
