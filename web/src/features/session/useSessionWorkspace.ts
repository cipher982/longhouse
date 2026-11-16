import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  applyPushedProjectionDelta,
  heldTranscriptRevision,
  pushedProjectionDeltaMayApply,
  refreshAgentSessionProjectionTail,
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
// How long an ingest wake marked "follows" waits for its pushed rows before
// reading them over HTTP. The server reads them in the background, usually in
// well under a second; past this the push is late or lost.
const PUSHED_DELTA_WAIT_MS = 2_000;
// Header refreshes start at most this often. A wake can arrive several times a
// second during a turn; once each header read took ~0.2 s the lane ran back to
// back (~2 reads a second per tab). The header is a status bar: a second of
// lag is invisible, and the transcript has its own revision-driven lane.
export const BASE_REFRESH_MIN_INTERVAL_MS = 1_000;
const INITIAL_EVENTS_PAGE_SIZE = 200;
const AUTO_SCROLL_MAX_ATTEMPTS = 12;
const AUTO_SCROLL_EPSILON_PX = 1;
/** Fallback polling interval when SSE stream is disconnected.
 *  Short so a broken stream still delivers updates within SLA ceiling. */
const WORKSPACE_FALLBACK_REFRESH_MS =
  (typeof window !== "undefined" && window.__TEST_WORKSPACE_FALLBACK_MS__) ||
  5_000;

/** Backoff for a transcript round that failed while the page is behind:
 *  1 s, 2 s, 4 s ... capped, until a round lands or a newer report starts one. */
const TRANSCRIPT_RETRY_BASE_MS = 1_000;
const TRANSCRIPT_RETRY_MAX_MS = 15_000;

/**
 * A running tool call's state follows the session, not a transcript write:
 * the server reads it as "dropped" once the session closes and "completed"
 * once a Cursor run ends (storage_v2_workspace._event_projection). The page
 * applies the same rule to the rows it holds, so closing a session settles
 * its spinners with no read.
 */
export function settleRunningTools(
  items: AgentSessionProjectionItem[],
  session: Pick<AgentSession, "provider" | "runtime_display" | "session_state"> | null,
): AgentSessionProjectionItem[] {
  if (!session) return items;
  const closed = session.runtime_display?.lifecycle === "closed";
  const cursorRunEnded =
    session.provider === "cursor" && session.session_state?.run?.lifecycle === "ended";
  if (!closed && !cursorRunEnded) return items;
  const settled: "completed" | "dropped" = cursorRunEnded ? "completed" : "dropped";
  let changed = false;
  const next = items.map((item) => {
    if (item.event?.tool_call_state !== "running") return item;
    changed = true;
    return { ...item, event: { ...item.event, tool_call_state: settled } };
  });
  return changed ? next : items;
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
  // Thinking text is held beside the reply preview, not in it: the reply row
  // keeps the last reply while the model thinks, and one live item still wins
  // (a non-thinking frame, ingest or session change clears this).
  const [streamThinking, setStreamThinking] =
    useState<SessionTranscriptPreview | null>(null);
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
    setStreamThinking(null);
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
    headerRefresh: true,
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

      if (streamConnected) {
        // Machine-control connect/disconnect is process-local state, not a
        // durable workspace mutation, so it does not wake the session SSE
        // stream. Keep a focused Console workspace polling until that state
        // has its own fanout contract; otherwise "Control offline" can remain
        // stale forever after the Machine Agent reconnects.
        if (currentSession.session_state.mode === "console") {
          return WORKSPACE_FALLBACK_REFRESH_MS;
        }
        return false;
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

  // Transcript sync (control-plane spec transcript-revision-sync.md): the page
  // holds the transcript at a revision, and every source that reports the
  // server's revision (the stream's frames, a workspace snapshot) goes through
  // one comparison. The transcript is read when, and only when, the server's
  // revision is newer than the one held.
  const observeTranscriptRevisionRef = useRef<
    ((revision: number | null | undefined, options?: { follows?: boolean }) => void) | null
  >(null);

  // SSE stream subscription — refreshes queries on server-side change detection
  useEffect(() => {
    // Always reset connection state at effect entry; the replacement stream
    // hasn't confirmed yet, so the fallback poll should stay armed until the
    // fresh onConnected fires.
    setStreamConnected(false);

    if (!sessionId || !documentVisible) {
      return;
    }

    // Only the header: once the transcript pages are held this is a
    // header-only read (useAgentSessionWorkspace headerRefresh). The rail's
    // session list keeps its own refresh; reading it on every wake was an
    // 800 ms catalog scan per update (catalog-read-path.md).
    const baseRefreshQueryKeys = [["agent-session-workspace", sessionId]] as const;
    // Read beside the transcript whenever it is read. The paged transcript
    // itself is refreshed by refreshAgentSessionProjectionTail, never
    // invalidated: an invalidation would drop the pages above the tail.
    const transcriptRefreshQueryKeys = [["session-subagents", sessionId]] as const;
    // Two independent coalescing lanes. Every wake refreshes the workspace
    // snapshot; the transcript lane runs only when the server reports a newer
    // transcript. Each lane runs one round at a time and queues at most one
    // more, so a burst of wakes costs two reads, and neither lane waits for the
    // other (stranger run F6).
    const base: { inFlight: boolean; queued: boolean; lastStartedAt: number; timer: number | null } = {
      inFlight: false,
      queued: false,
      lastStartedAt: Number.NEGATIVE_INFINITY,
      timer: null,
    };
    const transcript = {
      inFlight: false,
      queued: false,
      // The newest revision a round now running (or queued) is sure to cover:
      // it started after that revision was reported, and reads only rise.
      covers: 0,
      retirePreview: false,
      // Consecutive failed rounds, for the retry backoff.
      failures: 0,
    };
    let disposed = false;
    let latestReported = 0;
    let retryTimer: number | null = null;

    const applyStreamTranscriptPreview = (
      transcriptPreview: SessionTranscriptPreview | null,
      { keepThinking = false }: { keepThinking?: boolean } = {},
    ) => {
      setStreamTranscriptPreview(transcriptPreview);
      if (!keepThinking) setStreamThinking(null);
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
    const retireReplyPreview = () => {
      transcript.retirePreview = false;
      // Retiring the reply preview after an ingest must not take a live
      // thinking row with it: the prompt's own ingest lands while thinking.
      applyStreamTranscriptPreview(null, { keepThinking: true });
    };
    let freshnessTimer: number | null = null;
    const armFreshnessDeadline = () => {
      setStreamConnected(true);
      if (freshnessTimer !== null) window.clearTimeout(freshnessTimer);
      freshnessTimer = window.setTimeout(() => {
        freshnessTimer = null;
        if (!disposed) setStreamConnected(false);
      }, STREAM_FRAME_FRESHNESS_MS);
    };

    const runBase = () => {
      if (base.inFlight || base.timer !== null) {
        base.queued = base.queued || base.inFlight;
        return;
      }
      // Wakes inside the interval collapse into one refresh at its end.
      const wait = base.lastStartedAt + BASE_REFRESH_MIN_INTERVAL_MS - Date.now();
      if (wait > 0) {
        base.timer = window.setTimeout(() => {
          base.timer = null;
          if (!disposed) runBase();
        }, wait);
        return;
      }
      base.inFlight = true;
      base.lastStartedAt = Date.now();
      void Promise.all(
        baseRefreshQueryKeys.map((queryKey) =>
          queryClient.invalidateQueries({ queryKey }, { cancelRefetch: false }),
        ),
      ).finally(() => {
        base.inFlight = false;
        if (disposed || !base.queued) return;
        base.queued = false;
        runBase();
      });
    };

    // A frame marked "follows" is trailed by a projection_delta frame with the
    // newest rows. The transcript waits for a pushed delta that reaches the
    // reported revision, then reads over HTTP if none came in time. The wait
    // runs from the first frame it covers, so a steady run of frames cannot
    // postpone the read forever.
    let deltaWait: number | null = null;
    let awaitedRevision = 0;
    const stopWaitingForDelta = () => {
      awaitedRevision = 0;
      if (deltaWait === null) return;
      window.clearTimeout(deltaWait);
      deltaWait = null;
    };
    const behind = (revision: number) => {
      const held = heldTranscriptRevision(queryClient, sessionId);
      return held === null || revision > held;
    };

    const runTranscript = () => {
      if (transcript.inFlight) {
        transcript.queued = true;
        return;
      }
      stopWaitingForDelta();
      transcript.inFlight = true;
      transcript.covers = latestReported;
      void Promise.all([
        refreshAgentSessionProjectionTail(queryClient, sessionId),
        ...transcriptRefreshQueryKeys.map((queryKey) =>
          queryClient.invalidateQueries({ queryKey }, { cancelRefetch: false }),
        ),
      ])
        .then(([tailRead]) => tailRead, () => false)
        .then((tailRead) => {
          transcript.inFlight = false;
          if (disposed) return;
          if (transcript.queued) {
            transcript.queued = false;
            runTranscript();
            return;
          }
          if (transcript.retirePreview) retireReplyPreview();
          // Still behind what the server reported (the read failed, ran out
          // its deadline, or saw an older snapshot): try again with backoff.
          // A turn's last reply may be the last report for a long while, so
          // waiting for the next one is not enough.
          if (!behind(latestReported) || latestReported === 0) {
            transcript.failures = 0;
            return;
          }
          transcript.failures = tailRead ? 0 : transcript.failures + 1;
          if (retryTimer !== null) return;
          const delay = Math.min(
            TRANSCRIPT_RETRY_BASE_MS * 2 ** Math.max(0, transcript.failures - 1),
            TRANSCRIPT_RETRY_MAX_MS,
          );
          retryTimer = window.setTimeout(() => {
            retryTimer = null;
            if (!disposed && behind(latestReported)) runTranscript();
          }, delay);
        });
    };

    /**
     * The one rule. `revision` is the server's transcript revision from any
     * source; null means a source that should carry one did not (an older
     * server, or a catalog read that failed), which reads to be safe.
     */
    const observeTranscriptRevision = (
      revision: number | null | undefined,
      { follows = false }: { follows?: boolean } = {},
    ) => {
      if (typeof revision === "number") {
        latestReported = Math.max(latestReported, revision);
        if (!behind(revision)) {
          if (transcript.retirePreview && !transcript.inFlight && deltaWait === null) {
            retireReplyPreview();
          }
          return;
        }
        // A round already running started after this revision was reported.
        if (transcript.inFlight && revision <= transcript.covers) return;
      }
      if (
        follows &&
        typeof revision === "number" &&
        !transcript.inFlight &&
        pushedProjectionDeltaMayApply(queryClient, sessionId)
      ) {
        awaitedRevision = Math.max(awaitedRevision, revision);
        if (deltaWait === null) {
          deltaWait = window.setTimeout(() => {
            deltaWait = null;
            const awaited = awaitedRevision;
            awaitedRevision = 0;
            if (!disposed && behind(awaited)) runTranscript();
            else if (!disposed && transcript.retirePreview) retireReplyPreview();
          }, PUSHED_DELTA_WAIT_MS);
        }
        return;
      }
      runTranscript();
    };
    observeTranscriptRevisionRef.current = observeTranscriptRevision;

    const cleanup = connectSessionWorkspaceStream(
      sessionId,
      {
        onConnected: (data) => {
          streamEpochRef.current = data?.stream_epoch ?? null;
          recordServerClockSkew(data?.server_now_ms);
          armFreshnessDeadline();
          // A fresh attachment (a reconnect, a tab shown again, a new server
          // process) learns where the transcript is here, whatever it missed.
          observeTranscriptRevision(data?.transcript_revision);
        },
        onHeartbeat: () => {
          activityFeed.markHeartbeat();
          armFreshnessDeadline();
        },
        onReplayGap: () => {
          // Frames were lost: the snapshot is re-read, and the transcript
          // follows the revision the next frame reports.
          runBase();
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
          if (
            data.transcript_preview?.kind === "thinking" &&
            shouldRenderTranscriptPreview(data.transcript_preview)
          ) {
            setStreamThinking(data.transcript_preview);
            pendingRenderBeaconRef.current = {
              sessionId,
              latestEventId: data.latest_event_id,
              latestEventEmittedAtMs: data.latest_event_emitted_at_ms ?? null,
              serverFanoutAtMs: data.server_fanout_at_ms ?? null,
              clientReceivedAtMs: Date.now(),
              pubsubSeq: data.pubsub_seq ?? null,
            };
            setPendingRenderBeaconVersion((version) => version + 1);
            return;
          }
          // An ingest does not clear the live thinking row: the user's own
          // prompt is ingested a moment after thinking starts, and clearing it
          // there dropped the row (and its `t` key) until the next frame.
          // projectionItemsWithTranscriptPreview retires it once a durable
          // event as new as the thinking lands.
          const hasTranscriptPreview = Object.prototype.hasOwnProperty.call(
            data,
            "transcript_preview",
          );
          const transcriptPreview = data.transcript_preview ?? null;
          const isFreshTranscriptPreview =
            transcriptPreview !== null &&
            shouldRenderTranscriptPreview(transcriptPreview);
          // Frames that speak for the reply preview: an ingest, a preview, and
          // the attach frame. A runtime wake's copy is not authoritative.
          const speaksForPreview =
            !data.change_kind ||
            data.change_kind === "ingest" ||
            data.change_kind === "transcript_preview";
          if (hasTranscriptPreview && (speaksForPreview || isFreshTranscriptPreview)) {
            if (transcriptPreview === null && data.change_kind === "ingest") {
              // The durable rows that replace the preview are not on the page
              // until the transcript round lands; clearing it now blanked the
              // live reply for a whole fetch.
              transcript.retirePreview = true;
            } else {
              transcript.retirePreview = false;
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

          // A live preview is provisional text ahead of its durable write: it
          // says nothing about the transcript, so it neither reads it nor
          // re-reads the snapshot.
          if (isFreshTranscriptPreview) return;
          runBase();
          // The attach frame and every wake after a catalog read carry the
          // revision. An ingest frame without one could not read the catalog,
          // so it reads to be safe; other frames without one are not about
          // the transcript.
          const carriesRevision = Object.prototype.hasOwnProperty.call(
            data,
            "transcript_revision",
          );
          if (carriesRevision || data.change_kind === "ingest") {
            observeTranscriptRevision(data.transcript_revision, {
              follows: data.projection_delta === "follows",
            });
          }
        },
        onProjectionDelta: (data) => {
          armFreshnessDeadline();
          // Rows no newer than the held tail: already here, or overtaken.
          // They must not cancel a wait for newer ones.
          const deltaRevision = data.delta.transcript_revision;
          if (typeof deltaRevision === "number" && !behind(deltaRevision)) return;
          if (!applyPushedProjectionDelta(queryClient, sessionId, data.delta)) {
            // Not applicable here: read over HTTP instead.
            if (deltaWait !== null) runTranscript();
            return;
          }
          // The rows are on the page (or were already). Stop waiting once
          // the held transcript reaches what the frames reported.
          if (deltaWait !== null && !behind(awaitedRevision)) {
            stopWaitingForDelta();
            for (const queryKey of transcriptRefreshQueryKeys) {
              void queryClient.invalidateQueries({ queryKey }, { cancelRefetch: false });
            }
            if (transcript.retirePreview && !transcript.inFlight) retireReplyPreview();
          }
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
        previewKinds: ["thinking"],
        projectionDelta: true,
        skipInitial: workspaceReady,
        knownWorkspaceFingerprint: knownWorkspaceFingerprintRef.current,
        ...(streamEpochRef.current
          ? { streamEpoch: streamEpochRef.current }
          : {}),
      },
    );

    return () => {
      disposed = true;
      observeTranscriptRevisionRef.current = null;
      if (freshnessTimer !== null) window.clearTimeout(freshnessTimer);
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      if (base.timer !== null) window.clearTimeout(base.timer);
      stopWaitingForDelta();
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
  // A workspace snapshot reports the server's revision too: while the stream
  // is down it is the polled source, and it costs nothing when it is up.
  const workspaceTranscriptRevision =
    workspaceData?.transcript_revision ??
    workspaceData?.projection?.transcript_revision ??
    null;
  useEffect(() => {
    if (workspaceTranscriptRevision !== null) {
      observeTranscriptRevisionRef.current?.(workspaceTranscriptRevision);
    }
  }, [workspaceTranscriptRevision]);
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
    () =>
      projectionItemsWithTranscriptPreview(
        settleRunningTools(projectionItems, session),
        session,
        streamThinking,
      ),
    [projectionItems, session, streamThinking],
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
    subagents,
    selectedKey,
    selectedSelection,
    selectKey,
    handleVisibleSelectionChange,
    registerTimelineList,
    streamConnected,
    activityFeed,
  };
}
