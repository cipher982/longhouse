import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  holdsOlderPages,
  refreshAgentSessionProjectionTail,
  sessionHoldsOlderProjectionPages,
  useAgentSessionProjectionInfinite,
  useAgentSessionWorkspace,
} from "@/shared/api/useAgentSessions";
import { useDocumentVisible } from "@/shared/hooks/useDocumentVisible";
import { useOnlineEpoch } from "./useOnlineEpoch";
import {
  emitRenderBeacon,
  emitStateRenderBeacon,
  recordServerClockSkew,
} from "./renderBeacon";
import { isSessionClosed } from "@/shared/session/sessionRuntime";
import { activityClaimIsStale } from "@/shared/session/activityEvidence";
import {
  SessionActivityFeed,
  classifyWorkspaceChange,
} from "./sessionActivityFeed";
import {
  buildTimelineModel,
  getPreferredSelectionKey,
  projectionItemsWithTranscriptPreview,
  shouldRenderTranscriptPreview,
  timelineItemContainsSelection,
} from "@/shared/session/model";
import {
  connectSessionWorkspaceStream,
  type AgentEventId,
  type AgentSession,
  type AgentSessionProjectionItem,
  type AgentSessionProjectionResponse,
  type AgentSessionWorkspaceResponse,
  type SessionTranscriptPreview,
  fetchSessionSubagents,
} from "@/shared/api/agents";

const STREAM_FRAME_FRESHNESS_MS = 45_000;
const INITIAL_EVENTS_PAGE_SIZE = 200;
const AUTO_SCROLL_MAX_ATTEMPTS = 12;
const AUTO_SCROLL_EPSILON_PX = 1;
/** Fallback polling interval when SSE stream is disconnected.
 *  Short so a broken stream still delivers updates within SLA ceiling. */
const WORKSPACE_FALLBACK_REFRESH_MS =
  (typeof window !== "undefined" && window.__TEST_WORKSPACE_FALLBACK_MS__) ||
  5_000;

/** Slow reconciliation interval used even when SSE is connected.
 *  Server flips unpaired tool calls older than DROPPED_TOOL_AGE (1h) to
 *  "dropped" lazily on read; if SSE stays quiet (no other event in the session)
 *  the client otherwise never re-asks. ~60s keeps tool_call_state honest with
 *  small overhead. */
const WORKSPACE_RECONCILE_REFRESH_MS = 60_000;

function workspaceHasRunningTool(
  workspace:
    | { projection?: { items?: AgentSessionProjectionItem[] | null } | null }
    | null
    | undefined,
): boolean {
  const items = workspace?.projection?.items;
  if (!items) return false;
  for (const item of items) {
    if (item.kind === "event" && item.event?.tool_call_state === "running") {
      return true;
    }
  }
  return false;
}

interface UseSessionWorkspaceOptions {
  highlightEventId?: AgentEventId | null;
}

interface PendingRenderBeacon {
  sessionId: string;
  latestEventId: AgentEventId;
  latestEventEmittedAtMs: number | null;
  serverFanoutAtMs: number | null;
  clientReceivedAtMs: number | null;
  pubsubSeq: number | null;
}

interface PendingStateRenderBeacon {
  sessionId: string;
  catalogCommitSeq: number;
  serverFanoutAtMs: number | null;
  clientReceivedAtMs: number | null;
  pubsubSeq: number | null;
}

function getProjectionItemKey(item: AgentSessionProjectionItem): string {
  if (item.kind === "event" && item.event) {
    return `event:${item.event.id}`;
  }
  if (item.kind === "action" && item.action) {
    return `action:${item.action.id}`;
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

  // Closed and inactive sessions cannot keep polling because of a stale
  // pending interaction or activity signal.
  return (
    !isSessionClosed(session) &&
    (session.user_state == null || session.user_state === "active")
  );
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
  const [streamTranscriptPreview, setStreamTranscriptPreview] = useState<
    SessionTranscriptPreview | null | undefined
  >(undefined);
  const streamEpochRef = useRef<string | null>(null);
  const pendingRenderBeaconRef = useRef<PendingRenderBeacon | null>(null);
  const pendingStateRenderBeaconRef = useRef<PendingStateRenderBeacon | null>(
    null,
  );
  const [pendingRenderBeaconVersion, setPendingRenderBeaconVersion] =
    useState(0);
  const [pendingStateRenderBeaconVersion, setPendingStateRenderBeaconVersion] =
    useState(0);
  // One frame per stream wake, kept out of React state on purpose: the
  // activity strip subscribes directly and repaints its own canvas.
  const activityFeedRef = useRef<SessionActivityFeed | null>(null);
  if (activityFeedRef.current === null) {
    activityFeedRef.current = new SessionActivityFeed();
  }
  const activityFeed = activityFeedRef.current;

  useEffect(() => {
    setStreamTranscriptPreview(undefined);
    pendingRenderBeaconRef.current = null;
    pendingStateRenderBeaconRef.current = null;
    activityFeed.reset();
  }, [sessionId, activityFeed]);

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
      if (!documentVisible) {
        return false;
      }
      const currentSession = query.state.data?.session;
      // Keep re-asking after a failed refresh so a brief outage heals itself.
      if (query.state.error) {
        return WORKSPACE_FALLBACK_REFRESH_MS;
      }
      if (!currentSession) {
        return false;
      }
      if (!shouldRefreshWorkspaceSession(currentSession)) {
        return false;
      }

      // An activity window is retired by the reader's clock, not by a server
      // frame, so a quiet stream can still be rendering work the server has
      // already stopped asserting. Re-ask so the server answers with its own
      // last-seen label instead of leaving the client's stale claim on screen.
      // `activityClaimIsStale` goes false once that answer lands, so a wedged
      // session costs one refresh per cadence, not a permanent poll.
      if (
        activityClaimIsStale(
          currentSession.session_state.activity,
          Date.now(),
        )
      ) {
        return WORKSPACE_FALLBACK_REFRESH_MS;
      }

      // Slow reconciliation: server flips unpaired tool calls to "dropped"
      // after 1h on demand, so we re-ask occasionally even when SSE is quiet.
      if (streamConnected) {
        // Machine-control connect/disconnect is process-local state, not a
        // durable workspace mutation, so it does not wake the session SSE
        // stream. Keep a focused Console workspace polling until that state
        // has its own fanout contract; otherwise "Control offline" can remain
        // stale forever after the Machine Agent reconnects.
        if (currentSession.session_state.mode === "console") {
          return WORKSPACE_FALLBACK_REFRESH_MS;
        }
        return workspaceHasRunningTool(query.state.data)
          ? WORKSPACE_RECONCILE_REFRESH_MS
          : false;
      }

      // Stream is down: poll at the shorter fallback cadence.
      return WORKSPACE_FALLBACK_REFRESH_MS;
    },
  });
  const knownWorkspaceFingerprint =
    workspaceData?.workspace_revision?.fingerprint ?? null;
  const knownWorkspaceFingerprintRef = useRef(knownWorkspaceFingerprint);
  knownWorkspaceFingerprintRef.current = knownWorkspaceFingerprint;
  const workspaceReady = workspaceData !== undefined;

  // SSE stream subscription — invalidates queries on server-side change detection
  useEffect(() => {
    // Always reset connection state at effect entry; the replacement stream
    // hasn't confirmed yet, so the fallback poll should stay armed until the
    // fresh onConnected fires.
    setStreamConnected(false);

    if (!sessionId || !documentVisible) {
      return;
    }

    const baseRefreshQueryKeys = [
      ["agent-session-workspace", sessionId],
      ["agent-session", sessionId],
      ["agent-session-thread", sessionId],
      ["agent-sessions"],
    ] as const;
    // The paged transcript is refreshed by refreshAgentSessionProjectionTail,
    // not invalidated: an invalidation would drop the pages above the tail.
    const transcriptRefreshQueryKeys = [
      ["agent-session-events", sessionId],
      ["agent-session-events-infinite", sessionId],
      ["session-subagents", sessionId],
    ] as const;
    // Two independent coalescing lanes. Every wake refreshes the workspace
    // snapshot; only transcript mutations also refresh the transcript. Each
    // lane waits only for its own in-flight round (an invalidation joins an
    // in-flight fetch, which may predate the change, so a wake that lands
    // mid-round must run one more). Sharing one round made an ingest wake wait
    // for whatever workspace refetch a runtime wake had just started: during a
    // live turn runtime wakes arrive several per second, so on a slow Runtime
    // Host the durable echo of a send and its reply reached the page a full
    // workspace round late (stranger run F6).
    const lanes = {
      base: { keys: baseRefreshQueryKeys, inFlight: false, queued: false },
      transcript: {
        keys: transcriptRefreshQueryKeys,
        refreshProjectionTail: true,
        inFlight: false,
        queued: false,
        retirePreview: false,
      },
    };
    const applyStreamTranscriptPreview = (
      transcriptPreview: SessionTranscriptPreview | null,
    ) => {
      setStreamTranscriptPreview(transcriptPreview);
      queryClient.setQueriesData<AgentSessionWorkspaceResponse>(
        { queryKey: ["agent-session-workspace", sessionId] },
        (current) => {
          if (!current) return current;
          return {
            ...current,
            session: applyTranscriptPreviewToSession(
              current.session,
              transcriptPreview,
            ),
            thread: {
              ...current.thread,
              sessions: current.thread.sessions.map((item) =>
                item.id === sessionId
                  ? applyTranscriptPreviewToSession(item, transcriptPreview)
                  : item,
              ),
            },
          };
        },
      );
    };
    let disposed = false;
    let freshnessTimer: number | null = null;
    const armFreshnessDeadline = () => {
      setStreamConnected(true);
      if (freshnessTimer !== null) window.clearTimeout(freshnessTimer);
      freshnessTimer = window.setTimeout(() => {
        freshnessTimer = null;
        if (!disposed) setStreamConnected(false);
      }, STREAM_FRAME_FRESHNESS_MS);
    };
    const runLane = (lane: {
      keys: readonly (readonly unknown[])[];
      refreshProjectionTail?: boolean;
      inFlight: boolean;
      queued: boolean;
      retirePreview?: boolean;
    }) => {
      if (lane.inFlight) {
        lane.queued = true;
        return;
      }
      lane.inFlight = true;
      void Promise.all([
        ...lane.keys.map((queryKey) =>
          queryClient.invalidateQueries({ queryKey }, { cancelRefetch: false }),
        ),
        ...(lane.refreshProjectionTail
          ? [refreshAgentSessionProjectionTail(queryClient, sessionId)]
          : []),
      ]).finally(() => {
        lane.inFlight = false;
        if (disposed) return;
        if (lane.queued) {
          lane.queued = false;
          runLane(lane);
        } else if (lane.retirePreview) {
          lane.retirePreview = false;
          applyStreamTranscriptPreview(null);
        }
      });
    };
    const refreshWorkspaceQueries = (includeTranscript: boolean) => {
      runLane(lanes.base);
      if (includeTranscript) runLane(lanes.transcript);
    };
    // A transcript paged above its tail opts out of mount and focus refetches
    // (they would drop those pages), so reopening or refocusing the session
    // refreshes its tail here instead. Session open stays a durable refresh.
    if (sessionHoldsOlderProjectionPages(queryClient, sessionId)) {
      runLane(lanes.transcript);
    }

    const cleanup = connectSessionWorkspaceStream(
      sessionId,
      {
        onConnected: (data) => {
          const previousEpoch = streamEpochRef.current;
          const nextEpoch = data?.stream_epoch ?? null;
          streamEpochRef.current = nextEpoch;
          recordServerClockSkew(data?.server_now_ms);
          armFreshnessDeadline();
          if (previousEpoch && nextEpoch && previousEpoch !== nextEpoch) {
            refreshWorkspaceQueries(true);
          }
        },
        onHeartbeat: () => {
          activityFeed.markHeartbeat();
          armFreshnessDeadline();
        },
        onReplayGap: () => {
          refreshWorkspaceQueries(true);
        },
        onWorkspaceChanged: (data) => {
          recordServerClockSkew(data?.server_now_ms);
          armFreshnessDeadline();
          const frameKind = classifyWorkspaceChange(data);
          if (frameKind) activityFeed.push(frameKind);
          if (data.catalog_commit_seq != null && data.catalog_commit_seq > 0) {
            pendingStateRenderBeaconRef.current = {
              sessionId,
              catalogCommitSeq: data.catalog_commit_seq,
              serverFanoutAtMs: data.server_fanout_at_ms ?? null,
              clientReceivedAtMs: Date.now(),
              pubsubSeq: data.pubsub_seq ?? null,
            };
            setPendingStateRenderBeaconVersion((value) => value + 1);
          }
          const hasTranscriptPreview = Object.prototype.hasOwnProperty.call(
            data,
            "transcript_preview",
          );
          const transcriptPreview = data.transcript_preview ?? null;
          const isFreshTranscriptPreview =
            transcriptPreview !== null &&
            shouldRenderTranscriptPreview(transcriptPreview);
          const isTranscriptMutation =
            data.change_kind === "ingest" ||
            (data.change_kind === "transcript_preview" &&
              !isFreshTranscriptPreview) ||
            (!data.change_kind &&
              (data.latest_event_id > 0 || hasTranscriptPreview));
          if (
            hasTranscriptPreview &&
            (isTranscriptMutation || isFreshTranscriptPreview)
          ) {
            if (transcriptPreview === null && data.change_kind === "ingest") {
              // The durable rows that replace the preview are not on the page
              // until the transcript round lands; clearing it now blanked the
              // live reply for a whole fetch.
              lanes.transcript.retirePreview = true;
            } else {
              lanes.transcript.retirePreview = false;
              applyStreamTranscriptPreview(transcriptPreview);
            }
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

          if (isFreshTranscriptPreview) {
            // The preview is already applied to the workspace cache and local
            // projection. Wait for the durable ingest wake before refetching
            // transcript queries; runtime wakes must not evict it.
            return;
          }
          refreshWorkspaceQueries(isTranscriptMutation);
        },
        onError: () => {
          if (freshnessTimer !== null) {
            window.clearTimeout(freshnessTimer);
            freshnessTimer = null;
          }
          setStreamConnected(false);
        },
      },
      {
        skipInitial: workspaceReady,
        knownWorkspaceFingerprint: knownWorkspaceFingerprintRef.current,
        ...(streamEpochRef.current
          ? { streamEpoch: streamEpochRef.current }
          : {}),
      },
    );

    return () => {
      disposed = true;
      if (freshnessTimer !== null) window.clearTimeout(freshnessTimer);
      cleanup();
    };
  }, [
    sessionId,
    documentVisible,
    workspaceReady,
    queryClient,
    onlineEpoch,
    activityFeed,
  ]);
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
        item.id === sessionId
          ? applyTranscriptPreviewToSession(item, streamTranscriptPreview)
          : item,
      ),
    };
  }, [workspaceData?.thread, sessionId, streamTranscriptPreview]);
  const projectionFallbackPollMs =
    streamConnected ||
    !documentVisible ||
    !shouldRefreshWorkspaceSession(workspaceData?.session)
      ? false
      : WORKSPACE_FALLBACK_REFRESH_MS;
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
    refetchInterval: projectionFallbackPollMs,
  });
  // The query's own poll stands down once older pages are loaded; keep the
  // same cadence for the tail alone.
  const holdsOlderProjectionPages = holdsOlderPages(projectionPagesData);
  useEffect(() => {
    if (!sessionId || !projectionFallbackPollMs || !holdsOlderProjectionPages) {
      return;
    }
    const timer = window.setInterval(() => {
      void refreshAgentSessionProjectionTail(queryClient, sessionId);
    }, projectionFallbackPollMs);
    return () => window.clearInterval(timer);
  }, [sessionId, projectionFallbackPollMs, holdsOlderProjectionPages, queryClient]);

  const [manualSelectedKey, setManualSelectedKey] = useState<string | null>(
    null,
  );
  // Tracks which key is actually visible after TimelinePane's local filtering
  const [filteredVisibleKey, setFilteredVisibleKey] = useState<
    string | null | undefined
  >(undefined);
  const [timelineListElement, setTimelineListElement] =
    useState<HTMLDivElement | null>(null);
  const [evictedTailItems, setEvictedTailItems] = useState<
    AgentSessionProjectionItem[]
  >([]);
  const highlightedEventRef = useRef<AgentEventId | null>(null);
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
    if (projectionPagesData.pages.some((page) => page.generation_id)) {
      return projectionPagesData.pages;
    }
    return [...projectionPagesData.pages].sort(
      (left, right) => (left.page_offset ?? 0) - (right.page_offset ?? 0),
    );
  }, [projectionPagesData]);

  useEffect(() => {
    const tailPage =
      sortedProjectionPages.length > 0
        ? sortedProjectionPages[sortedProjectionPages.length - 1]
        : (workspaceData?.projection ?? null);
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

    setEvictedTailItems((current) =>
      mergeProjectionItems(current, droppedItems),
    );
  }, [sortedProjectionPages, workspaceData?.projection]);

  const projectionItems = useMemo(() => {
    if (sortedProjectionPages.length === 0) return evictedTailItems;

    const tailPage = sortedProjectionPages[sortedProjectionPages.length - 1];
    const historicalItems = sortedProjectionPages
      .slice(0, -1)
      .flatMap((page) => page.items);

    return mergeProjectionItems(
      historicalItems,
      evictedTailItems,
      tailPage.items,
    );
  }, [sortedProjectionPages, evictedTailItems]);

  // Count rendered transcript entries (events and actions), not seam dividers.
  const loadedEntryCount = useMemo(
    () =>
      projectionItems.filter(
        (item) => item.kind === "event" || item.kind === "action",
      ).length,
    [projectionItems],
  );

  const totalEntries = useMemo(
    // The tail page is the one every refresh replaces, so it carries the
    // current counts; older pages keep the counts from when they loaded.
    () => sortedProjectionPages.at(-1)?.total ?? projectionItems.length,
    [projectionItems.length, sortedProjectionPages],
  );

  const abandonedEvents = useMemo(
    () => sortedProjectionPages.at(-1)?.abandoned_events ?? 0,
    [sortedProjectionPages],
  );

  const visibleProjectionItems = useMemo(
    () => projectionItemsWithTranscriptPreview(projectionItems, session),
    [projectionItems, session],
  );
  // Workers this session spawned. Hidden from the timeline by design, so the
  // transcript is the only place they surface — attached to the tool call that
  // spawned them rather than listed as separate sessions.
  const { data: subagentsData } = useQuery({
    queryKey: ["session-subagents", sessionId],
    queryFn: () => fetchSessionSubagents(sessionId as string),
    enabled: Boolean(sessionId),
    staleTime: 60_000,
  });
  const subagents = useMemo(
    () => subagentsData?.children ?? [],
    [subagentsData],
  );

  const model = useMemo(
    () => buildTimelineModel(visibleProjectionItems, subagents),
    [visibleProjectionItems, subagents],
  );
  const events = model.events;

  const threadSessions = useMemo(
    () => threadData?.sessions || (session ? [session] : []),
    [threadData, session],
  );

  const headSessionId =
    threadData?.head_session_id ||
    session?.thread_head_session_id ||
    session?.id ||
    null;

  const currentThreadSession = useMemo(
    () =>
      threadSessions.find((item) => item.id === session?.id) || session || null,
    [threadSessions, session],
  );

  const headThreadSession = useMemo(
    () =>
      threadSessions.find((item) => item.id === headSessionId) ||
      currentThreadSession,
    [threadSessions, headSessionId, currentThreadSession],
  );

  useEffect(() => {
    const pending = pendingRenderBeaconRef.current;
    if (!pending || pending.sessionId !== sessionId) return;
    if (!pending.latestEventEmittedAtMs) return;
    const latestEventIsRendered = events.some(
      (event) => event.id === pending.latestEventId,
    );
    if (!latestEventIsRendered) return;

    const caps = currentThreadSession?.capabilities;
    const managed = Boolean(
      caps && (caps.live_control_available || caps.host_reattach_available),
    );
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

  useEffect(() => {
    const pending = pendingStateRenderBeaconRef.current;
    if (!pending || pending.sessionId !== sessionId) return;
    const currentSession = workspaceData?.session;
    if (!currentSession) return;
    const renderedCommitSeq = currentSession?.session_state.commit_seq;
    if (
      renderedCommitSeq == null ||
      renderedCommitSeq < pending.catalogCommitSeq
    )
      return;

    const caps = currentSession?.capabilities;
    const managed = Boolean(
      caps && (caps.live_control_available || caps.host_reattach_available),
    );
    const observedAt = currentSession.session_state.activity.observed_at;
    emitStateRenderBeacon({
      sessionId: pending.sessionId,
      catalogCommitSeq: pending.catalogCommitSeq,
      statePhase: currentSession.session_state.activity.state,
      stateObservedAtMs: observedAt ? Date.parse(observedAt) : null,
      managed,
      serverFanoutAtMs: pending.serverFanoutAtMs,
      clientReceivedAtMs: pending.clientReceivedAtMs,
      pubsubSeq: pending.pubsubSeq,
    });
    pendingStateRenderBeaconRef.current = null;
  }, [pendingStateRenderBeaconVersion, sessionId, workspaceData?.session]);

  const isViewingHead =
    !!currentThreadSession &&
    !!headThreadSession &&
    currentThreadSession.id === headThreadSession.id;

  const resolvedHighlightEventId = useMemo(() => {
    if (highlightEventId == null) return null;
    return (
      events.find((event) => String(event.id) === String(highlightEventId))
        ?.id ?? null
    );
  }, [highlightEventId, events]);
  const hasHighlightEvent =
    highlightEventId == null || resolvedHighlightEventId != null;

  const highlightSelectionKey = useMemo(() => {
    if (highlightEventId == null || !hasHighlightEvent) {
      return null;
    }
    return resolvedHighlightEventId == null
      ? null
      : (model.eventIdToSelectionKey.get(resolvedHighlightEventId) ?? null);
  }, [
    highlightEventId,
    hasHighlightEvent,
    resolvedHighlightEventId,
    model.eventIdToSelectionKey,
  ]);

  const visibleManualSelectedKey = useMemo(() => {
    if (model.items.length === 0 || manualSelectedKey == null) {
      return null;
    }

    return model.items.some((item) =>
      timelineItemContainsSelection(item, manualSelectedKey),
    )
      ? manualSelectedKey
      : null;
  }, [model.items, manualSelectedKey]);

  const selectedKey = highlightSelectionKey ?? visibleManualSelectedKey;

  useEffect(() => {
    if (highlightEventId == null) return;
    if (hasHighlightEvent) return;
    if (!hasPreviousPage || isFetchingPreviousPage) return;
    void fetchPreviousPage();
  }, [
    highlightEventId,
    hasHighlightEvent,
    hasPreviousPage,
    isFetchingPreviousPage,
    fetchPreviousPage,
  ]);

  useEffect(() => {
    if (highlightEventId == null) return;
    if (!hasHighlightEvent) return;
    if (highlightedEventRef.current === highlightEventId) return;

    const rowId =
      resolvedHighlightEventId == null
        ? null
        : model.eventIdToRowId.get(resolvedHighlightEventId);

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
  }, [
    highlightEventId,
    hasHighlightEvent,
    resolvedHighlightEventId,
    model.eventIdToRowId,
  ]);

  useEffect(() => {
    if (highlightEventId != null) return;
    if (projectionLoading) return;
    if (autoScrolledSelectionRef.current) return;
    if (model.items.length === 0) return;
    const fallbackItem =
      [...model.items]
        .reverse()
        .find((item) => getPreferredSelectionKey(item)) ?? null;
    const targetKey =
      selectedKey ||
      (fallbackItem ? getPreferredSelectionKey(fallbackItem) : null);
    const selection = targetKey
      ? (model.selectionMap.get(targetKey) ?? null)
      : null;

    if (selectedKey && !selection) return;

    let frameId: number | null = null;
    let attempts = 0;

    const tryScrollToSelection = () => {
      attempts += 1;

      if (!selectedKey) {
        const list = timelineListElement;
        if (list instanceof HTMLElement) {
          const maxScrollTop = Math.max(
            0,
            list.scrollHeight - list.clientHeight,
          );
          if (maxScrollTop > AUTO_SCROLL_EPSILON_PX) {
            list.scrollTop = maxScrollTop;
            if (list.scrollTop > AUTO_SCROLL_EPSILON_PX) {
              autoScrolledSelectionRef.current = true;
              return;
            }
          }

          if (attempts >= AUTO_SCROLL_MAX_ATTEMPTS) {
            autoScrolledSelectionRef.current =
              maxScrollTop <= AUTO_SCROLL_EPSILON_PX;
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
  }, [
    highlightEventId,
    projectionLoading,
    selectedKey,
    model.items,
    model.selectionMap,
    timelineListElement,
  ]);

  // When TimelinePane reports a filtered visible key, use that for the inspector.
  // `undefined` means no filter callback has fired yet (treat as unfiltered).
  const effectiveSelectedKey =
    filteredVisibleKey === undefined ? selectedKey : filteredVisibleKey;

  const selectedSelection = useMemo(
    () =>
      effectiveSelectedKey
        ? (model.selectionMap.get(effectiveSelectedKey) ?? null)
        : null,
    [effectiveSelectedKey, model.selectionMap],
  );

  const selectKey = (key: string) => {
    setManualSelectedKey(key);
  };

  const handleVisibleSelectionChange = useCallback(
    (visibleKey: string | null) => {
      setFilteredVisibleKey(visibleKey);
    },
    [],
  );

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
    loadedEntryCount,
    abandonedEvents,
    eventsLoading: projectionLoading,
    eventsError: projectionError,
    controlOnly: workspaceData?.control_only ?? false,
    fetchPreviousPage,
    hasPreviousPage,
    isFetchingPreviousPage,
    items: model.items,
    selectedKey,
    selectedSelection,
    selectKey,
    handleVisibleSelectionChange,
    registerTimelineList,
    streamConnected,
    activityFeed,
  };
}
