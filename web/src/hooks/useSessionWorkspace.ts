import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  useAgentSessionProjectionInfinite,
  useAgentSessionWorkspace,
} from "./useAgentSessions";
import { useDocumentVisible } from "./useDocumentVisible";
import { resolveSessionRuntimeState } from "../lib/sessionRuntime";
import {
  buildTimelineModel,
  getPreferredSelectionKey,
  timelineItemContainsSelection,
} from "../lib/sessionWorkspace";
import { connectSessionWorkspaceStream } from "../services/api/agents";

const INITIAL_EVENTS_PAGE_SIZE = 200;
const AUTO_SCROLL_MAX_ATTEMPTS = 12;
const AUTO_SCROLL_EPSILON_PX = 1;
/** Fallback polling interval when SSE stream is disconnected. */
const WORKSPACE_FALLBACK_REFRESH_MS = 30_000;

interface UseSessionWorkspaceOptions {
  highlightEventId?: number | null;
}

export function useSessionWorkspace(
  sessionId: string | null,
  options: UseSessionWorkspaceOptions = {},
) {
  const highlightEventId = options.highlightEventId ?? null;
  const documentVisible = useDocumentVisible();
  const queryClient = useQueryClient();
  const [streamConnected, setStreamConnected] = useState(false);

  // SSE stream subscription — invalidates queries on server-side change detection
  useEffect(() => {
    if (!sessionId || !documentVisible) {
      setStreamConnected(false);
      return;
    }

    const cleanup = connectSessionWorkspaceStream(
      sessionId,
      {
        onConnected: () => setStreamConnected(true),
        onWorkspaceChanged: () => {
          void queryClient.invalidateQueries({ queryKey: ["agent-session", sessionId] });
          void queryClient.invalidateQueries({ queryKey: ["agent-session-thread", sessionId] });
          void queryClient.invalidateQueries({ queryKey: ["agent-session-projection-infinite", sessionId] });
          void queryClient.invalidateQueries({ queryKey: ["agent-session-events", sessionId] });
          void queryClient.invalidateQueries({ queryKey: ["agent-session-events-infinite", sessionId] });
          void queryClient.invalidateQueries({ queryKey: ["agent-sessions"] });
        },
        onError: () => setStreamConnected(false),
      },
      { skipInitial: true },
    );

    return cleanup;
  }, [sessionId, documentVisible, queryClient]);

  const [showAbandonedBranches, setShowAbandonedBranches] = useState(false);
  const branchMode = showAbandonedBranches ? "all" : "head";
  const {
    data: workspaceData,
    isLoading: sessionLoading,
    error: sessionError,
  } = useAgentSessionWorkspace(sessionId, {
    limit: INITIAL_EVENTS_PAGE_SIZE,
    branch_mode: branchMode,
    refetchInterval: (query) => {
      // When SSE stream is connected, no polling needed
      if (streamConnected) return false;

      // Fallback: poll at reduced frequency when stream is down
      const currentSession = query.state.data?.session;
      if (!documentVisible || !currentSession) {
        return false;
      }

      const runtime = resolveSessionRuntimeState(currentSession);
      const shouldRefresh =
        currentSession.ended_at == null ||
        runtime.isLive ||
        runtime.needsAttention ||
        runtime.heuristicActive;

      return shouldRefresh ? WORKSPACE_FALLBACK_REFRESH_MS : false;
    },
  });
  const session = workspaceData?.session ?? null;
  const threadData = workspaceData?.thread ?? null;
  const {
    data: projectionPagesData,
    isLoading: projectionLoading,
    error: projectionError,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useAgentSessionProjectionInfinite(sessionId, {
    limit: INITIAL_EVENTS_PAGE_SIZE,
    branch_mode: branchMode,
    enabled: Boolean(workspaceData),
    initialPage: workspaceData?.projection ?? null,
  });

  const [manualSelectedKey, setManualSelectedKey] = useState<string | null>(null);
  // Tracks which key is actually visible after TimelinePane's local filtering
  const [filteredVisibleKey, setFilteredVisibleKey] = useState<string | null | undefined>(undefined);
  const [timelineListElement, setTimelineListElement] = useState<HTMLDivElement | null>(null);
  const highlightedEventRef = useRef<number | null>(null);
  const autoScrolledSelectionRef = useRef(false);

  const registerTimelineList = useCallback((node: HTMLDivElement | null) => {
    setTimelineListElement((current) => (current === node ? current : node));
  }, []);

  const projectionItems = useMemo(
    () => projectionPagesData?.pages.flatMap((page) => page.items) || [],
    [projectionPagesData],
  );

  const totalEntries = useMemo(
    () => projectionPagesData?.pages[0]?.total ?? projectionItems.length,
    [projectionItems.length, projectionPagesData],
  );

  const abandonedEvents = useMemo(
    () => projectionPagesData?.pages[0]?.abandoned_events ?? 0,
    [projectionPagesData],
  );

  const model = useMemo(() => buildTimelineModel(projectionItems), [projectionItems]);
  const events = model.events;

  const threadSessions = useMemo(
    () => threadData?.sessions || (session ? [session] : []),
    [threadData, session],
  );

  const headSessionId =
    threadData?.head_session_id || session?.thread_head_session_id || session?.id || null;

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

  const hasHighlightEvent = useMemo(() => {
    if (highlightEventId == null) return true;
    return events.some((event) => event.id === highlightEventId);
  }, [highlightEventId, events]);

  const highlightSelectionKey = useMemo(() => {
    if (highlightEventId == null || !hasHighlightEvent) {
      return null;
    }
    return model.eventIdToSelectionKey.get(highlightEventId) ?? null;
  }, [highlightEventId, hasHighlightEvent, model.eventIdToSelectionKey]);

  const visibleManualSelectedKey = useMemo(() => {
    if (model.items.length === 0 || manualSelectedKey == null) {
      return null;
    }

    return model.items.some((item) => timelineItemContainsSelection(item, manualSelectedKey))
      ? manualSelectedKey
      : null;
  }, [model.items, manualSelectedKey]);

  const selectedKey = highlightSelectionKey ?? visibleManualSelectedKey;

  useEffect(() => {
    if (highlightEventId == null) return;
    if (hasHighlightEvent) return;
    if (!hasNextPage || isFetchingNextPage) return;
    void fetchNextPage();
  }, [highlightEventId, hasHighlightEvent, hasNextPage, isFetchingNextPage, fetchNextPage]);

  useEffect(() => {
    if (highlightEventId == null) return;
    if (!hasHighlightEvent) return;
    if (highlightedEventRef.current === highlightEventId) return;

    const rowId = model.eventIdToRowId.get(highlightEventId);

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
  }, [highlightEventId, hasHighlightEvent, model.eventIdToRowId]);

  useEffect(() => {
    if (highlightEventId != null) return;
    if (projectionLoading) return;
    if (autoScrolledSelectionRef.current) return;
    if (model.items.length === 0) return;
    const fallbackItem = [...model.items].reverse().find((item) => getPreferredSelectionKey(item)) ?? null;
    const targetKey = selectedKey || (fallbackItem ? getPreferredSelectionKey(fallbackItem) : null);
    const selection = targetKey ? model.selectionMap.get(targetKey) ?? null : null;

    if (selectedKey && !selection) return;

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
        if (!selection) return;
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
  }, [highlightEventId, projectionLoading, selectedKey, model.items, model.selectionMap, timelineListElement]);

  // When TimelinePane reports a filtered visible key, use that for the inspector.
  // `undefined` means no filter callback has fired yet (treat as unfiltered).
  const effectiveSelectedKey = filteredVisibleKey === undefined ? selectedKey : filteredVisibleKey;

  const selectedSelection = useMemo(
    () => (effectiveSelectedKey ? model.selectionMap.get(effectiveSelectedKey) ?? null : null),
    [effectiveSelectedKey, model.selectionMap],
  );

  const selectKey = (key: string) => {
    setManualSelectedKey(key);
  };

  const handleVisibleSelectionChange = useCallback((visibleKey: string | null) => {
    setFilteredVisibleKey(visibleKey);
  }, []);

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
    totalEntries,
    loadedEntryCount: projectionItems.length,
    abandonedEvents,
    eventsLoading: projectionLoading,
    eventsError: projectionError,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    items: model.items,
    selectedKey,
    selectedSelection,
    selectKey,
    handleVisibleSelectionChange,
    registerTimelineList,
  };
}
