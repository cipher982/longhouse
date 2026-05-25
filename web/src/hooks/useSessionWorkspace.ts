import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  useAgentSessionProjectionInfinite,
  useAgentSessionTurns,
  useAgentSessionWorkspace,
} from "./useAgentSessions";
import { useDocumentVisible } from "./useDocumentVisible";
import { useOnlineEpoch } from "./useOnlineEpoch";
import { emitRenderBeacon, recordServerClockSkew } from "../lib/renderBeacon";
import { isSessionClosed, resolveSessionRuntimeState } from "../lib/sessionRuntime";
import {
  buildTimelineModel,
  getPreferredSelectionKey,
  projectionItemsWithTranscriptPreview,
  shouldRenderTranscriptPreview,
  timelineItemContainsSelection,
} from "../lib/sessionWorkspace";
import {
  connectSessionWorkspaceStream,
  type AgentSession,
  type AgentSessionProjectionItem,
  type AgentSessionProjectionResponse,
  type AgentSessionWorkspaceResponse,
  type SessionTranscriptPreview,
} from "../services/api/agents";

const INITIAL_EVENTS_PAGE_SIZE = 200;
const AUTO_SCROLL_MAX_ATTEMPTS = 12;
const AUTO_SCROLL_EPSILON_PX = 1;
/** Fallback polling interval when SSE stream is disconnected.
 *  Short so a broken stream still delivers updates within SLA ceiling. */
const WORKSPACE_FALLBACK_REFRESH_MS =
  (typeof window !== "undefined" && window.__TEST_WORKSPACE_FALLBACK_MS__) || 5_000;

interface UseSessionWorkspaceOptions {
  highlightEventId?: number | null;
}

interface PendingRenderBeacon {
  sessionId: string;
  latestEventId: number;
  latestEventEmittedAtMs: number | null;
  serverFanoutAtMs: number | null;
  clientReceivedAtMs: number | null;
  pubsubSeq: number | null;
}

function getProjectionItemKey(item: AgentSessionProjectionItem): string {
  if (item.kind === "event" && item.event) {
    return `event:${item.event.id}`;
  }
  return `seam:${item.session_id}:${item.timestamp}`;
}

function mergeProjectionItems(
  ...itemGroups: AgentSessionProjectionItem[][]
): AgentSessionProjectionItem[] {
  const merged: AgentSessionProjectionItem[] = [];
  const seen = new Set<string>();

  for (const items of itemGroups) {
    for (const item of items) {
      const key = getProjectionItemKey(item);
      if (seen.has(key)) continue;
      seen.add(key);
      merged.push(item);
    }
  }

  return merged;
}

function shouldRefreshWorkspaceSession(
  session: AgentSession | null | undefined,
): boolean {
  if (!session) {
    return false;
  }

  // lifecycle==='closed' is the ground-truth closure signal. Keep polling
  // while the session is open, or when a live/attention signal is still present.
  const runtime = resolveSessionRuntimeState(session);
  if (!isSessionClosed(session)) {
    return true;
  }
  return runtime.isLive || runtime.needsAttention;
}

function applyTranscriptPreviewToSession(
  session: AgentSession,
  transcriptPreview: SessionTranscriptPreview | null,
): AgentSession {
  return {
    ...session,
    transcript_preview: transcriptPreview,
  };
}

export function useSessionWorkspace(
  sessionId: string | null,
  options: UseSessionWorkspaceOptions = {},
) {
  const highlightEventId = options.highlightEventId ?? null;
  const documentVisible = useDocumentVisible();
  const onlineEpoch = useOnlineEpoch();
  const queryClient = useQueryClient();
  const [streamConnected, setStreamConnected] = useState(false);
  const [streamTranscriptPreview, setStreamTranscriptPreview] =
    useState<SessionTranscriptPreview | null | undefined>(undefined);
  const pendingRenderBeaconRef = useRef<PendingRenderBeacon | null>(null);
  const [pendingRenderBeaconVersion, setPendingRenderBeaconVersion] = useState(0);

  useEffect(() => {
    setStreamTranscriptPreview(undefined);
  }, [sessionId]);

  // SSE stream subscription — invalidates queries on server-side change detection
  useEffect(() => {
    // Always reset connection state at effect entry; the replacement stream
    // hasn't confirmed yet, so the fallback poll should stay armed until the
    // fresh onConnected fires.
    setStreamConnected(false);

    if (!sessionId || !documentVisible) {
      return;
    }

    const cleanup = connectSessionWorkspaceStream(
      sessionId,
      {
        onConnected: (data) => {
          recordServerClockSkew(data?.server_now_ms);
          setStreamConnected(true);
        },
        onWorkspaceChanged: (data) => {
          recordServerClockSkew(data?.server_now_ms);
          let shouldDeferRefetchForPreview = false;
          if (Object.prototype.hasOwnProperty.call(data, "transcript_preview")) {
            const transcriptPreview = data.transcript_preview ?? null;
            shouldDeferRefetchForPreview = shouldRenderTranscriptPreview(transcriptPreview);
            setStreamTranscriptPreview(transcriptPreview);
            queryClient.setQueriesData<AgentSessionWorkspaceResponse>(
              { queryKey: ["agent-session-workspace", sessionId] },
              (current) => {
                if (!current) return current;
                return {
                  ...current,
                  session: applyTranscriptPreviewToSession(current.session, transcriptPreview),
                  thread: {
                    ...current.thread,
                    sessions: current.thread.sessions.map((item) =>
                      item.id === sessionId ? applyTranscriptPreviewToSession(item, transcriptPreview) : item,
                    ),
                  },
                };
              },
            );
          }

          pendingRenderBeaconRef.current = {
            sessionId,
            latestEventId: data.latest_event_id,
            latestEventEmittedAtMs: data.latest_event_emitted_at_ms ?? null,
            serverFanoutAtMs: data.server_fanout_at_ms ?? null,
            clientReceivedAtMs: Date.now(),
            pubsubSeq: data.pubsub_seq ?? null,
          };
          setPendingRenderBeaconVersion((version) => version + 1);

          const refreshWorkspaceQueries = () => {
            void queryClient.invalidateQueries({ queryKey: ["agent-session-workspace", sessionId] });
            void queryClient.invalidateQueries({ queryKey: ["agent-session", sessionId] });
            void queryClient.invalidateQueries({ queryKey: ["agent-session-thread", sessionId] });
            void queryClient.invalidateQueries({ queryKey: ["agent-session-turns", sessionId] });
            void queryClient.invalidateQueries({ queryKey: ["agent-session-projection-infinite", sessionId] });
            void queryClient.invalidateQueries({ queryKey: ["agent-session-events", sessionId] });
            void queryClient.invalidateQueries({ queryKey: ["agent-session-events-infinite", sessionId] });
            void queryClient.invalidateQueries({ queryKey: ["agent-sessions"] });
          };

          if (shouldDeferRefetchForPreview && typeof window !== "undefined" && window.requestAnimationFrame) {
            window.requestAnimationFrame(() => {
              window.setTimeout(refreshWorkspaceQueries, 0);
            });
          } else {
            refreshWorkspaceQueries();
          }
        },
        onError: () => setStreamConnected(false),
      },
      { skipInitial: true },
    );

    return cleanup;
  }, [sessionId, documentVisible, queryClient, onlineEpoch]);

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
      if (!documentVisible || !shouldRefreshWorkspaceSession(currentSession)) {
        return false;
      }
      return WORKSPACE_FALLBACK_REFRESH_MS;
    },
  });
  const rawSession = workspaceData?.session ?? null;
  const session = useMemo(
    () =>
      rawSession && streamTranscriptPreview !== undefined
        ? applyTranscriptPreviewToSession(rawSession, streamTranscriptPreview)
        : rawSession,
    [rawSession, streamTranscriptPreview],
  );
  const threadData = useMemo(() => {
    const rawThread = workspaceData?.thread ?? null;
    if (!rawThread || !sessionId || streamTranscriptPreview === undefined) {
      return rawThread;
    }
    return {
      ...rawThread,
      sessions: rawThread.sessions.map((item) =>
        item.id === sessionId ? applyTranscriptPreviewToSession(item, streamTranscriptPreview) : item,
      ),
    };
  }, [workspaceData?.thread, sessionId, streamTranscriptPreview]);
  const {
    data: turnsData,
    isLoading: turnsLoading,
    error: turnsError,
  } = useAgentSessionTurns(sessionId, {
    limit: 10,
    order: "desc",
    // Phase 3: lifecycle (with terminal_state fallback) gates done-ness.
    enabled: Boolean(sessionId && session && !isSessionClosed(session)),
    refetchInterval:
      streamConnected || !documentVisible || !shouldRefreshWorkspaceSession(session)
        ? false
        : WORKSPACE_FALLBACK_REFRESH_MS,
  });
  const {
    data: projectionPagesData,
    isLoading: projectionLoading,
    error: projectionError,
    fetchPreviousPage,
    hasPreviousPage,
    isFetchingPreviousPage,
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
  const [evictedTailItems, setEvictedTailItems] = useState<AgentSessionProjectionItem[]>([]);
  const highlightedEventRef = useRef<number | null>(null);
  const autoScrolledSelectionRef = useRef(false);
  const lastTailPageRef = useRef<AgentSessionProjectionResponse | null>(null);

  const registerTimelineList = useCallback((node: HTMLDivElement | null) => {
    setTimelineListElement((current) => (current === node ? current : node));
  }, []);

  useEffect(() => {
    setEvictedTailItems([]);
    lastTailPageRef.current = null;
  }, [sessionId, branchMode]);

  const sortedProjectionPages = useMemo(() => {
    if (!projectionPagesData) return [];
    return [...projectionPagesData.pages]
      .sort((left, right) => (left.page_offset ?? 0) - (right.page_offset ?? 0));
  }, [projectionPagesData]);

  useEffect(() => {
    const tailPage =
      sortedProjectionPages.length > 0
        ? sortedProjectionPages[sortedProjectionPages.length - 1]
        : workspaceData?.projection ?? null;
    if (!tailPage) return;

    const previousTailPage = lastTailPageRef.current;
    lastTailPageRef.current = tailPage;

    if (!previousTailPage) return;

    const previousOffset = previousTailPage.page_offset ?? 0;
    const currentOffset = tailPage.page_offset ?? 0;
    if (currentOffset <= previousOffset) return;

    const evictedCount = currentOffset - previousOffset;
    const droppedItems = previousTailPage.items.slice(0, evictedCount);
    if (droppedItems.length === 0) return;

    setEvictedTailItems((current) => mergeProjectionItems(current, droppedItems));
  }, [sortedProjectionPages, workspaceData?.projection]);

  const projectionItems = useMemo(() => {
    if (sortedProjectionPages.length === 0) return evictedTailItems;

    const tailPage = sortedProjectionPages[sortedProjectionPages.length - 1];
    const historicalItems = sortedProjectionPages
      .slice(0, -1)
      .flatMap((page) => page.items);

    return mergeProjectionItems(historicalItems, evictedTailItems, tailPage.items);
  }, [sortedProjectionPages, evictedTailItems]);

  // Count only actual event items (not seam dividers) so the "X/Y loaded"
  // counter matches what the backend reports as entries.
  const loadedEventCount = useMemo(
    () => projectionItems.filter((item) => item.kind === "event").length,
    [projectionItems],
  );

  const totalEntries = useMemo(
    () => projectionPagesData?.pages[0]?.total ?? projectionItems.length,
    [projectionItems.length, projectionPagesData],
  );

  const abandonedEvents = useMemo(
    () => projectionPagesData?.pages[0]?.abandoned_events ?? 0,
    [projectionPagesData],
  );

  const visibleProjectionItems = useMemo(
    () => projectionItemsWithTranscriptPreview(projectionItems, session),
    [projectionItems, session],
  );
  const model = useMemo(() => buildTimelineModel(visibleProjectionItems), [visibleProjectionItems]);
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

  useEffect(() => {
    const pending = pendingRenderBeaconRef.current;
    if (!pending || pending.sessionId !== sessionId) return;
    if (!pending.latestEventEmittedAtMs) return;
    const latestEventIsRendered = events.some((event) => event.id === pending.latestEventId);
    if (!latestEventIsRendered) return;

    const caps = currentThreadSession?.capabilities;
    const managed = Boolean(caps && (caps.live_control_available || caps.host_reattach_available));
    emitRenderBeacon({
      sessionId: pending.sessionId,
      latestEventId: pending.latestEventId,
      latestEventEmittedAtMs: pending.latestEventEmittedAtMs,
      managed,
      serverFanoutAtMs: pending.serverFanoutAtMs,
      clientReceivedAtMs: pending.clientReceivedAtMs,
      pubsubSeq: pending.pubsubSeq,
    });
    pendingRenderBeaconRef.current = null;
  }, [pendingRenderBeaconVersion, events, sessionId, currentThreadSession]);

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
    if (!hasPreviousPage || isFetchingPreviousPage) return;
    void fetchPreviousPage();
  }, [highlightEventId, hasHighlightEvent, hasPreviousPage, isFetchingPreviousPage, fetchPreviousPage]);

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
    turns: turnsData?.turns ?? [],
    turnsLoading,
    turnsError,
    threadSessions,
    headSessionId,
    currentThreadSession,
    headThreadSession,
    isViewingHead,
    showAbandonedBranches,
    setShowAbandonedBranches,
    events,
    totalEntries,
    loadedEntryCount: loadedEventCount,
    abandonedEvents,
    eventsLoading: projectionLoading,
    eventsError: projectionError,
    fetchPreviousPage,
    hasPreviousPage,
    isFetchingPreviousPage,
    items: model.items,
    selectedKey,
    selectedSelection,
    selectKey,
    handleVisibleSelectionChange,
    registerTimelineList,
  };
}
