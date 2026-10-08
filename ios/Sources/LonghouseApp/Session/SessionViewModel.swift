import Foundation
import SwiftUI

@MainActor
final class SessionViewModel: ObservableObject {
    private struct PendingRealtimeTelemetry {
        let latestEventId: Int
        let serverFanoutAtMs: Int64?
        let clientReceivedAtMs: Int64
        let clockSkewMs: Int
        let catalogCommitSeq: Int64?
        let pubsubSeq: Int?
    }
    private static let workingConsoleTurnStates: Set<String> = [
        "starting",
        "active",
        "draining",
    ]

    @Published var detail: SessionDetail? {
        didSet { refreshQueuedIndicator() }
    }
    /// Viewer transport only. A connected stream is not provider liveness.
    @Published private(set) var realtimeConnection: SessionRealtimeConnection = .disconnected
    @Published private(set) var hostUpdateState = HostUpdateState()
    @Published private(set) var hostUpdateNow = Date()
    // Benchmark-only attribution. These deliberately are not @Published: the
    // subsequent transcript mutation owns the SwiftUI invalidation, preventing
    // an extra render of the previous snapshot under the next revision number.
    private(set) var benchmarkSourceRevision: Int?
    private(set) var benchmarkSourceOperation: String?
    /// Bumped by every mutation of a transcript payload input. Handing the
    /// transcript to WebKit means JSON-encoding and base64ing every row, and
    /// SwiftUI re-runs the session screen's body — and so `updateUIView` — on
    /// every invalidation, a composer keystroke included. This counter is how
    /// `WebTranscriptView` tells "the parent changed" from "the transcript
    /// changed". Keep it on every stored property the payload reads.
    private(set) var transcriptRevision: UInt64 = 0
    /// Only inputs consumed by `TimelineBuilder` invalidate an in-flight tail
    /// build. Worker metadata and renderer errors affect WebKit payloads, but
    /// cannot make a detached timeline build stale.
    private(set) var timelineBuildRevision: UInt64 = 0
    /// Version of the durable projection inputs, independent of `items`.
    /// Detached builders must not publish rows from before a tail/history
    /// commit that happened while they were suspended.
    private(set) var transcriptInputRevision: UInt64 = 0

    @Published var items: [TimelineItem] = [] {
        didSet {
            transcriptRevision &+= 1
            timelineBuildRevision &+= 1
        }
    }
    /// Workers this session spawned, attached to the tool rows that spawned them.
    @Published var subagents: [SessionSubagent] = [] { didSet { transcriptRevision &+= 1 } }
    /// Full tool bodies loaded for rows a lite page sent as previews.
    @Published private(set) var liteBodies = LiteBodyState() { didSet { transcriptRevision &+= 1 } }
    /// Blocking load error: only set when there is genuinely nothing to show
    /// (no cache, never loaded). Drives the full-screen error overlay.
    @Published var errorMessage: String? { didSet { transcriptRevision &+= 1 } }
    /// Non-blocking refresh failure: set when a reconnect/refresh fails but we
    /// already have cached content on screen. Drives a thin banner over the
    /// transcript instead of erasing it.
    @Published var refreshErrorMessage: String?
    @Published var isInitialLoading = true
    /// True after a cached or network transcript snapshot has been accepted.
    /// It stays true for a valid empty session, and gates the composer during
    /// cold-load errors so metadata cannot make sends appear prematurely.
    @Published private(set) var hasLoadedTranscript = false
    /// The watermark attached to the payload whose frame WebKit has actually
    /// presented. It is intentionally nil while a newer payload is in flight.
    @Published private(set) var renderedTranscriptReadThrough: String?
    /// Watermark for the accepted transcript payload currently being prepared.
    /// It travels with the render receipt; session metadata alone cannot prove
    /// that these rows have appeared on screen.
    @Published private(set) var transcriptReadThrough: String?
    /// True only after the mounted transcript document has presented a frame.
    /// Having cached rows is not the same thing as having pixels on screen.
    @Published private(set) var isTranscriptFrameReady = false
    /// A failed frame acknowledgement must reveal a retryable native error,
    /// not leave the restoring surface spinning forever.
    @Published private(set) var transcriptRendererErrorMessage: String?
    /// Monotonic retry nonce used to force WebKit to render an unchanged
    /// payload again after a frame acknowledgement failure.
    @Published private(set) var transcriptRenderRetryRevision: UInt64 = 0
    @Published var isSending = false
    @Published private(set) var isStoppingConsoleWork = false
    @Published var stopErrorMessage: String?
    @Published var isRespondingToPauseRequest = false
    /// Frames received on the workspace stream, for the dock's activity strip.
    let activity = ActivityPulseStore()
    @Published var pauseResponseErrorMessage: String?
    @Published var resumeIntent: SessionResumeIntent?
    @Published var branchMessage: String = ""
    @Published var isBranching = false
    @Published var branchErrorMessage: String?
    /// Set when a branch starts, so the view can follow it.
    @Published var branchedSessionId: String?
    @Published var isPreparingResume = false
    @Published var resumeErrorMessage: String?
    private var transcriptDiagnostics: RenderBeaconReporter.WebKitDiagnostics?
    /// Most recent send outcome so the UI can distinguish an immediate
    /// dispatch from a queued input without pretending the latter was sent.
    @Published var lastSendOutcome: SessionInputOutcome?
    @Published var queuedInputCount: Int = 0
    /// How many of those were queued by another sender (an agent's `continue`,
    /// the web, directed input, another device): real, but not visible as a
    /// bubble here, so the count needs to say so.
    @Published var queuedElsewhereCount: Int = 0
    @Published var failedInputCount: Int = 0
    @Published var submittedInputs: [SubmittedInput] = [] { didSet { transcriptRevision &+= 1 } }
    /// Identity-bound prompt for the one rejected steer awaiting an explicit
    /// Queue instead decision. Its transcript row is hidden while this prompt
    /// is visible, so one operation has one visible owner.
    @Published var turnEndedDraft: TurnEndedInput?
    /// Monotonic counter; each send increments it. Used so a delayed "Sent."
    /// auto-dismiss task only clears the label it owns.
    private(set) var sendCounter: UInt64 = 0

    private var pollTask: Task<Void, Never>?
    private var prefetchTask: Task<Void, Never>?
    private var realtimeRefreshRetryTask: Task<Void, Never>?
    private var primaryDetailTask: Task<Void, Never>?
    /// A native-chrome wake that arrives while the primary request is in
    /// flight gets one coalesced follow-up instead of being lost.
    private var primaryDetailRefreshPending = false
    private var primaryDetailRequestToken = 0
    private var subagentsTask: Task<Void, Never>?
    private var subagentsRefreshPending = false
    private var subagentsRequestToken = 0
    private var routeLoadGeneration = 0
    private var realtimePaused = false
    private var tailRefreshTask: Task<Void, Error>?
    private var activeTailRefreshToken: Int?
    private var nextTailRefreshToken = 0
    private var realtimeRefreshFailureCount = 0
    private var stream: SessionWorkspaceStreamSource?
    private var streamTask: Task<Void, Never>?
    private var streamConnected: Bool = false
    /// The server process epoch last accepted by this route. A pubsub cursor
    /// from another epoch cannot be used to infer transcript continuity.
    private var streamEpoch: String?

    /// Guards against an auth-refresh→reconnect→401 loop: we attempt at most
    /// one refresh per stream session, reset once a connection succeeds.
    private var streamAuthRefreshAttempted = false
    var hasRealtimeStreamTaskForTesting: Bool { streamTask != nil }
    /// True while a durable tail refresh is in flight. The realtime wake loop
    /// reads this as "a refresh is still running" and re-arms on it, so a
    /// refresh that has finished must never leave this true: the loop would
    /// join a completed task, never suspend, and pin the main actor forever.
    var hasTailRefreshInFlightForTesting: Bool { tailRefreshTask != nil }
    private var pendingRealtimeTelemetry: PendingRealtimeTelemetry?
    private var activeSessionId: String?
    private var activeServerURL: String?
    /// Auth/login generation is part of the pending-intent scope. A cookie
    /// rotation or tenant switch must not reuse the previous route's
    /// submitted rows or receipt reconciliation.
    private var activeAuthGeneration: String?
    private var lastWorkspaceEvents: [SessionEvent] = []
    private var lastWorkspaceProjectionItems: [SessionProjectionItem] = []
    /// What the last full tail read: a delta continues the held pages only
    /// from the same render generation and head session.
    private var heldTailGenerationId: String?
    private var heldTailHeadSessionId: String?
    private var lastFullTailReadAt: Date?
    private var loadedProjectionItemCount = 0
    private var totalProjectionItemCount = 0
    /// Older transcript rows exist that this view has not loaded.
    var hasOlderHistory: Bool { loadedProjectionItemCount < totalProjectionItemCount }
    private var tailSnapshotEventId: String?
    private var tailNextCursor: String?
    private var prefetchedOlderTail: SessionMobileTailResponse?
    /// The cursor the stored older page was fetched with. Prefetch identity is
    /// the cursor, not an offset: offsets are ignored by storage-v2 paging.
    private var prefetchedOlderCursor: String?
    private var prefetchedOlderSnapshotEventId: String?
    private var prefetchInFlightCursor: String?
    private var prefetchInFlightSnapshotEventId: String?
    private var prefetchInFlightToken: Int?
    private var nextPrefetchToken = 0
    private var isLoadingOlder = false
    private var realtimeRefreshTask: Task<Void, Never>?
    private var realtimeRefreshRequestToken = 0
    private var realtimeRefreshPending = false
    /// Wakes gathered for the next pass of the realtime loop. Only the
    /// stream's initial snapshot frame carries a watermark here; any other
    /// wake demands the conservative follow-up.
    private var realtimeWakeSnapshotEventId: Int?
    private var realtimeWakeNeedsFollowUp = false
    /// The newest durable event any tail response has shown, whether or not
    /// it was applied: what a joined refresh actually observed.
    private var tailObservedEventId: Int?
    private var historyFillStalledAtLoadedCount: Int?
    /// WebKit can measure a short document in the same callback that reports
    /// its first frame. Remember that request until MainActor records the frame.
    private var historyFillPendingFirstFrame = false
    private var openWaterfall: SessionOpenWaterfall?
    /// Preview updates can arrive faster than TimelineBuilder can assemble
    /// native rows. Keep one detached build in flight and replace only its
    /// pending input; cancellation of a wrapper cannot interrupt a detached
    /// synchronous build.
    private struct RealtimePreviewBuildRequest {
        let buildInput: [SessionProjectionItem]
        let preview: SessionTranscriptPreview?
        let transcriptReadThrough: String?
        var buildRevision: UInt64
        let sourceRevision: UInt64
        let routeGeneration: Int
        let sessionId: String
    }
    private var realtimePreviewBuildTask: Task<Void, Never>?
    private var pendingRealtimePreviewBuild: RealtimePreviewBuildRequest?
    private var pendingTranscriptReadThrough: String?
    /// Receipts from a previous session must never make a replacement route
    /// look ready, even if its retry nonce happens to match.
    private var transcriptRevisionFloor: UInt64 = 0
    /// Cache rows are an instant paint, not proof that the current network
    /// projection has been reconciled. The first accepted tail must publish
    /// even when the server fingerprint and preview happen to match the cache.
    private var transcriptRowsReconciled = false
    private var transcriptRowsPublishedPreview: SessionTranscriptPreview?
    private var detailWasLoadedFromTail = false
    private var detailWasLoadedFromPrimary = false
    private let apiFactory: (String) -> SessionWorkspaceClient?
    private let streamFactory: (URL, String, Int?, String?) -> SessionWorkspaceStreamSource
    private let enableRealtime: Bool
    /// Warm reopen and cold relaunch both come from here; the store owns which
    /// tier answers.
    private let snapshotStore: TranscriptSnapshotStore?
    /// Unresolved payloads and receipt-only summaries survive process death;
    /// the summary is removed only when its exact transcript echo is linked.
    private let pendingInputStore: PendingInputStore
    private let realtimeRefreshRetryDelaysNanoseconds: [UInt64]
    private var lastPubsubSeq: Int?
    private var lastWorkspaceRevisionFingerprint: String?
    private let initialTailLimit = 50
    /// Lite pages carry tool bodies as previews, so a 100-event older page
    /// costs about what 40 full events did; fewer, larger pages mean fewer
    /// walls on the way back through history.
    private let olderPageLimit = 100
    init(
        apiFactory: @escaping (String) -> SessionWorkspaceClient? = { LonghouseAPI(host: $0) },
        streamFactory: @escaping (URL, String, Int?, String?) -> SessionWorkspaceStreamSource = { baseURL, sessionId, sinceSeq, fingerprint in
            SessionWorkspaceStreamSource.live(
                baseURL: baseURL,
                sessionId: sessionId,
                sinceSeq: sinceSeq,
                knownWorkspaceFingerprint: fingerprint
            )
        },
        enableRealtime: Bool = true,
        snapshotStore: TranscriptSnapshotStore? = nil,
        pendingInputStore: PendingInputStore = .shared,
        realtimeRefreshRetryDelaysNanoseconds: [UInt64] = [
            1_000_000_000,
            2_000_000_000,
            5_000_000_000,
            10_000_000_000,
        ]
    ) {
        self.apiFactory = apiFactory
        self.streamFactory = streamFactory
        self.enableRealtime = enableRealtime
        self.snapshotStore = snapshotStore ?? (enableRealtime ? .shared : nil)
        self.pendingInputStore = pendingInputStore
        self.realtimeRefreshRetryDelaysNanoseconds = realtimeRefreshRetryDelaysNanoseconds
    }

    func start(sessionId: String, appState: AppState) async {
        let normalizedServerURL = TranscriptSnapshot.normalizedServerURL(appState.serverURL)
        let authGeneration = SharedAuthStore.authGeneration(for: normalizedServerURL)
        let sessionChanged = activeSessionId != sessionId
            || activeServerURL != normalizedServerURL
            || activeAuthGeneration != authGeneration
        if sessionChanged {
            routeLoadGeneration &+= 1
        }
        realtimePaused = false
        let startGeneration = routeLoadGeneration
        if !sessionChanged,
           tailRefreshTask != nil || primaryDetailTask != nil {
            // Scene activation can race the route task. The first opener
            // already owns cache/tail work; joining it here would read disk
            // again and wait on a second reload before the UI can settle.
            if enableRealtime, hasLoadedTranscript {
                if streamTask == nil {
                    startStream(sessionId: sessionId, appState: appState)
                }
                if pollTask == nil {
                    startVisiblePolling(sessionId: sessionId, appState: appState)
                }
            }
            return
        }
        var restoredFromCache = false
        var shouldRefreshCachedTail = false
        var initialTailTask: Task<Void, Never>?
        if sessionChanged {
            openWaterfall = SessionOpenWaterfall(sessionId: sessionId)
            if let api = apiFactory(appState.serverURL) {
                ClientDiagnosticsReporter.shared.sink = { payload in await api.postClientDiagnostics(payload) }
                // The channel has a sink now, so this is the first point the
                // previous run's outcome can actually be shipped.
                RunBreadcrumb.shared.reportPreviousRunIfNeeded()
            }
            transcriptReadThrough = nil
            hasLoadedTranscript = false
            activeSessionId = sessionId
            pendingTranscriptReadThrough = nil
            activeServerURL = normalizedServerURL
            activeAuthGeneration = authGeneration
            renderedTranscriptReadThrough = nil
            isInitialLoading = true
            isTranscriptFrameReady = false
            transcriptRendererErrorMessage = nil
            transcriptRenderRetryRevision = 0
            isStoppingConsoleWork = false
            stopErrorMessage = nil
            detail = nil
            detailWasLoadedFromTail = false
            detailWasLoadedFromPrimary = false

            items = []
            activity.reset()
            transcriptRowsReconciled = false
            subagents = []
            liteBodies = LiteBodyState()
            liteBodyGeneration &+= 1
            transcriptRowsPublishedPreview = nil
            submittedInputs = []
            let pendingAuthGeneration = SharedAuthStore.authGeneration(for: normalizedServerURL)
            let restoredPendingInputs = pendingInputStore.load(
                serverURL: normalizedServerURL,
                sessionId: sessionId,
                authGeneration: pendingAuthGeneration
            )
            restorePendingInputs(restoredPendingInputs)
            if !restoredPendingInputs.isEmpty {
                Task { [weak self] in
                    await self?.reconcilePendingInputs(
                        restoredPendingInputs,
                        sessionId: sessionId,
                        appState: appState,
                        authGeneration: pendingAuthGeneration
                    )
                }
            }
            loadedProjectionItemCount = 0
            totalProjectionItemCount = 0
            historyFillStalledAtLoadedCount = nil
            transcriptInputRevision &+= 1
            historyFillPendingFirstFrame = false
            transcriptDiagnostics = nil
            pendingRealtimeTelemetry = nil
            lastWorkspaceEvents = []
            lastWorkspaceProjectionItems = []
            heldTailGenerationId = nil
            heldTailHeadSessionId = nil
            lastFullTailReadAt = nil
            realtimeRefreshTask?.cancel()
            realtimeRefreshTask = nil
            realtimeRefreshPending = false
            realtimePreviewBuildTask?.cancel()
            pendingRealtimePreviewBuild = nil
            tailSnapshotEventId = nil
            tailNextCursor = nil
            prefetchedOlderTail = nil
            prefetchedOlderCursor = nil
            prefetchedOlderSnapshotEventId = nil
            prefetchInFlightCursor = nil
            prefetchInFlightSnapshotEventId = nil
            prefetchInFlightToken = nil
            prefetchTask?.cancel()
            prefetchTask = nil
            realtimeRefreshRetryTask?.cancel()
            realtimeRefreshRetryTask = nil
            tailRefreshTask?.cancel()
            tailRefreshTask = nil
            activeTailRefreshToken = nil
            cancelPrimaryDetailLoad()
            subagentsTask?.cancel()
            subagentsTask = nil
            lastPubsubSeq = nil
            lastWorkspaceRevisionFingerprint = nil
            streamEpoch = nil
            streamAuthRefreshAttempted = false
            realtimeWakeSnapshotEventId = nil
            realtimeWakeNeedsFollowUp = false
            tailObservedEventId = nil
            transcriptRevisionFloor = transcriptRevision
            refreshErrorMessage = nil
            pauseResponseErrorMessage = nil
            // Start compact metadata and transcript networking together before
            // touching durable cache I/O. Cache hydration may still win first,
            // but a cold disk read can never delay the tail request.
            if let api = apiFactory(appState.serverURL) {
                loadPrimaryDetail(api: api, sessionId: sessionId)
                initialTailTask = Task { [weak self] in
                    await self?.reload(
                        sessionId: sessionId,
                        appState: appState,
                        refreshSecondary: false
                    )
                }
            }
            // Realtime is a third, independent lane. It starts immediately
            // after the small cache read so a persisted resume cursor is
            // available when the stream is constructed. The primary detail
            // and tail requests are already running and never wait for this
            // local metadata hydration.
            // Warm path: the in-process tier survives backgrounding while the
            // process lives. Cold path: the durable on-disk tier survives app
            // eviction, so a relaunch into a session renders the last-seen
            // transcript instead of a blank screen with a lone warning icon.
            if let snapshotStore {
                let restored = await snapshotStore.loadAsync(
                    serverURL: appState.serverURL,
                    sessionId: sessionId
                )
                guard activeSessionId == sessionId,
                      routeLoadGeneration == startGeneration,
                      !Task.isCancelled,
                      !realtimePaused
                else { return }
                if let restored {
                    let ageMs = Int(Date().timeIntervalSince(restored.snapshot.savedAt) * 1000)
                    openWaterfall?.mark(
                        "cache_hit",
                        "tier=\(restored.tier.rawValue) events=\(restored.snapshot.events.count) age_ms=\(ageMs)"
                    )
                    if !hasLoadedTranscript {
                        if await applySnapshot(
                            restored,
                            sessionId: sessionId,
                            generation: startGeneration
                        ) {
                            // A snapshot is an instant paint, not the source
                            // of truth. The already-started tail will
                            // reconcile it.
                            restoredFromCache = true
                            shouldRefreshCachedTail = true
                        }
                    } else {
                        // The tail may win the transcript race before disk
                        // hydration finishes. It does not carry the persisted
                        // stream resume cursor, so retain only resume metadata
                        // that the fresh tail has not already replaced.
                        adoptResumeMetadata(from: restored.snapshot)
                        openWaterfall?.mark("cache_discarded", "reason=tail_won")
                    }
                } else {
                    openWaterfall?.mark("cache_miss")
                }
            } else {
                openWaterfall?.mark("cache_miss")
            }
            if enableRealtime {
                startStream(sessionId: sessionId, appState: appState)
                startVisiblePolling(sessionId: sessionId, appState: appState)
            }
        } else {
            if enableRealtime {
                // Re-entry into an unresolved route must attach before the
                // cache/tail join too; the transcript lane is not a gate for
                // live status or control affordances.
                if streamTask == nil {
                    startStream(sessionId: sessionId, appState: appState)
                }
                if pollTask == nil {
                    startVisiblePolling(sessionId: sessionId, appState: appState)
                }
            }
            activeServerURL = normalizedServerURL
            if let api = apiFactory(appState.serverURL),
               detail == nil,
               primaryDetailTask == nil {
                // Keep compact chrome independent from a cold cache read.
                loadPrimaryDetail(api: api, sessionId: sessionId)
            }
            if isTranscriptFrameReady,
               hasLoadedTranscript,
               let api = apiFactory(appState.serverURL) {
                // Re-entry is an explicit secondary-lane refresh. Stream
                // wakes and ordinary polls do not need to refetch workers.
                loadSubagents(api: api, sessionId: sessionId)
            }
            // A scene transition can interrupt the first disk hydration while
            // leaving the same route mounted. Retry that cache lookup on
            // re-entry; otherwise an offline resume falls through to a blank
            // network-only load despite a valid saved transcript.
            if !hasLoadedTranscript,
               items.isEmpty,
               let snapshotStore {
                let restored = await snapshotStore.loadAsync(
                    serverURL: appState.serverURL,
                    sessionId: sessionId
                )
                guard activeSessionId == sessionId,
                      routeLoadGeneration == startGeneration,
                      !Task.isCancelled,
                      !realtimePaused
                else { return }
                if let restored {
                    openWaterfall?.mark(
                        "cache_hit",
                        "tier=\(restored.tier.rawValue) events=\(restored.snapshot.events.count)"
                    )
                    if await applySnapshot(
                        restored,
                        sessionId: sessionId,
                        generation: startGeneration
                    ) {
                        restoredFromCache = true
                        shouldRefreshCachedTail = true
                    }
                } else {
                    openWaterfall?.mark("cache_miss")
                }
            }
        }
        if let api = apiFactory(appState.serverURL),
           detail == nil,
           primaryDetailTask == nil {
            // Resume/re-entry can arrive with transcript content but without
            // route metadata; keep the primary lane independently recoverable.
            loadPrimaryDetail(api: api, sessionId: sessionId)
        }
        let hasContentOnScreen = restoredFromCache || hasLoadedTranscript || !items.isEmpty

        if hasContentOnScreen {
            // We already have something to show (hydrated from cache/disk, or
            // preserved across a pause). Reconcile in the background so a
            // failed refresh degrades to a banner instead of erasing the
            // transcript. This is the path that fixes the lock/unlock blank.
            if let api = apiFactory(appState.serverURL) {
                if (shouldRefreshCachedTail || !sessionChanged),
                   initialTailTask == nil {
                    Task { [weak self] in
                        await self?.refreshInBackground(
                            api: api,
                            sessionId: sessionId,
                            generation: startGeneration
                        )
                    }
                }
            }
            isInitialLoading = false
        } else {
            // Cold loads await the already-started tail task. Cached opens
            // intentionally return after cache paint while that task runs.
            if let initialTailTask {
                await initialTailTask.value
            } else {
                await reload(sessionId: sessionId, appState: appState, refreshSecondary: false)
            }
        }
        guard activeSessionId == sessionId,
              routeLoadGeneration == startGeneration,
              !Task.isCancelled,
              !realtimePaused
        else { return }
        guard enableRealtime else { return }
        // Cache resume metadata has now been applied, if available. The
        // primary and tail requests already ran independently while it was
        // being read; this is only a malformed-URL/re-entry safety net.
        if streamTask == nil {
            startStream(sessionId: sessionId, appState: appState)
        }
        if pollTask == nil {
            startVisiblePolling(sessionId: sessionId, appState: appState)
        }
    }

    /// Tear down realtime work (SSE + polling + prefetch) WITHOUT discarding
    /// the session identity or the rendered transcript. Use this for scene
    /// background/inactive: SSE over URLSession is foreground-only, so we must
    /// drop the connection, but the next `.active` should resume the same
    /// session and keep its content rather than treating unlock as a brand-new
    /// session open (which is what erased the transcript before).
    func pauseRealtime() {
        openWaterfall?.mark("pause")
        routeLoadGeneration &+= 1
        realtimePaused = true
        // A metadata request has no value after the route leaves the
        // foreground. Cancel it with the rest of the route work; the next
        // active start will issue it again if the title is still unresolved.
        subagentsTask?.cancel()
        subagentsTask = nil
        subagentsRequestToken &+= 1
        subagentsRefreshPending = false
        cancelPrimaryDetailLoad()
        tailRefreshTask?.cancel()
        tailRefreshTask = nil
        activeTailRefreshToken = nil
        pollTask?.cancel()
        pollTask = nil
        prefetchTask?.cancel()
        prefetchTask = nil
        realtimeRefreshTask?.cancel()
        realtimeRefreshRequestToken &+= 1
        realtimeRefreshTask = nil
        realtimeRefreshPending = false
        realtimePreviewBuildTask?.cancel()
        pendingRealtimePreviewBuild = nil
        realtimeRefreshRetryTask?.cancel()
        realtimeRefreshRetryTask = nil
        realtimeRefreshFailureCount = 0
        prefetchInFlightCursor = nil
        prefetchInFlightSnapshotEventId = nil
        prefetchInFlightToken = nil
        streamTask?.cancel()
        streamTask = nil
        realtimeConnection = .disconnected
        if let oldStream = stream {
            Task { await oldStream.stop() }
        }
        stream = nil
        streamConnected = false
    }

    func handleMemoryWarning() {
        let hasPrefetch = prefetchedOlderTail != nil
        openWaterfall?.mark(
            "memory_warning",
            "events=\(lastWorkspaceEvents.count) items=\(items.count) has_prefetch=\(hasPrefetch)"
        )
        WebTranscriptWebViewPool.discardWarmSpare()
        prefetchTask?.cancel()
        prefetchTask = nil
        prefetchedOlderTail = nil
        prefetchedOlderCursor = nil
        prefetchedOlderSnapshotEventId = nil
        prefetchInFlightCursor = nil
        prefetchInFlightSnapshotEventId = nil
        prefetchInFlightToken = nil
    }

    /// Full teardown for genuine nav-away or session switch: stops realtime AND
    func stop() {
        openWaterfall?.mark("stop")
        openWaterfall = nil
        primaryDetailTask?.cancel()
        primaryDetailTask = nil
        detailWasLoadedFromTail = false
        ClientDiagnosticsReporter.shared.flush()
        pauseRealtime()
        activeSessionId = nil
        activeServerURL = nil
    }

    func reload(
        sessionId: String,
        appState: AppState,
        refreshSecondary: Bool = true
    ) async {
        let requestGeneration = routeLoadGeneration
        // If we already have content on screen, a failed reload must degrade to
        // the non-destructive banner. Only a truly empty view earns the
        // full-screen blocking error.
        let hasContent = hasLoadedTranscript || !items.isEmpty || !submittedInputs.isEmpty
        if !hasLoadedTranscript {
            isInitialLoading = true
        }
        guard let api = apiFactory(appState.serverURL) else {
            if hasContent {
                refreshErrorMessage = "Invalid server URL"
            } else {
                errorMessage = "Invalid server URL"
            }
            isInitialLoading = false
            return
        }
        if detail == nil, primaryDetailTask == nil {
            loadPrimaryDetail(api: api, sessionId: sessionId)
        }

        openWaterfall?.mark("reload_start")
        do {
            try await refreshTail(api: api, sessionId: sessionId)
            guard isCurrentRoute(sessionId: sessionId, generation: requestGeneration) else { return }
            if refreshSecondary, isTranscriptFrameReady || items.isEmpty {
                // Explicit pull-to-refresh/re-entry is an intentional worker
                // refresh. The initial tail is intentionally data-only; the
                // first-frame hook owns the opening worker request.
                loadSubagents(api: api, sessionId: sessionId)
            }
            if errorMessage != nil {
                errorMessage = nil
            }
            if refreshErrorMessage != nil {
                refreshErrorMessage = nil
            }
        } catch is CancellationError {
            return
        } catch LonghouseAPIError.notAuthenticated {
            guard isCurrentRoute(sessionId: sessionId, generation: requestGeneration) else { return }
            if hasContent || hasLoadedTranscript || !items.isEmpty || !submittedInputs.isEmpty {
                refreshErrorMessage = "Session expired. Pull to refresh."
            } else {
                errorMessage = "Session expired."
            }
        } catch {
            guard isCurrentRoute(sessionId: sessionId, generation: requestGeneration) else { return }
            if hasContent || hasLoadedTranscript || !items.isEmpty || !submittedInputs.isEmpty {
                refreshErrorMessage = "Live update temporarily unavailable. Showing saved messages."
            } else {
                errorMessage = "Couldn't load session. Pull to refresh."
            }
        }
        guard isCurrentRoute(sessionId: sessionId, generation: requestGeneration) else { return }
        isInitialLoading = false
    }

    /// Fetch the compact session detail independently from the transcript
    /// projection. The detail route is the primary tier for navigation chrome;
    /// a tail response that wins the race remains authoritative and prevents a
    /// late detail response from replacing newer state.
    private func isCurrentRoute(sessionId: String, generation: Int) -> Bool {
        activeSessionId == sessionId
            && routeLoadGeneration == generation
            && !realtimePaused
            && !Task.isCancelled
    }

    private func cancelPrimaryDetailLoad() {
        primaryDetailRequestToken &+= 1
        primaryDetailRefreshPending = false
        primaryDetailTask?.cancel()
        primaryDetailTask = nil
    }

    private func loadPrimaryDetail(api: SessionWorkspaceClient, sessionId: String) {
        // Compact detail is single-flight. A runtime/title wake arriving
        // while the opening request is in flight should not cancel that
        // request and pay a second handshake; one follow-up keeps the newest
        // chrome state from being lost.
        guard primaryDetailTask == nil else {
            primaryDetailRefreshPending = true
            return
        }
        primaryDetailRequestToken &+= 1
        let requestToken = primaryDetailRequestToken
        openWaterfall?.mark("detail_request_start")
        primaryDetailTask = Task { [weak self] in
            do {
                let loaded = try await api.sessionDetail(id: sessionId)
                guard !Task.isCancelled else { return }
                await MainActor.run {
                    guard let self,
                          self.activeSessionId == sessionId,
                          self.primaryDetailRequestToken == requestToken
                    else { return }
                    guard self.shouldAcceptPrimaryDetail(loaded) else {
                        self.openWaterfall?.mark("detail_discarded", "reason=older_primary")
                        return
                    }
                    if self.detailWasLoadedFromTail, let existing = self.detail {
                        // The compact lane serves no input receipts (only the
                        // workspace tail does); its empty list must not erase
                        // the queue line the tail last read.
                        var compact = loaded
                        compact.inputReceipts = nil
                        self.detail = compact.preservingOptionalEnrichment(from: existing)
                    } else {
                        self.detail = loaded
                    }
                    self.detailWasLoadedFromPrimary = true
                    self.openWaterfall?.mark(
                        "detail_loaded",
                        "title_chars=\(loaded.displayTitle.count)"
                    )
                }
            } catch is CancellationError {
                return
            } catch {
                await MainActor.run { [weak self] in
                    guard let self,
                          self.activeSessionId == sessionId,
                          self.primaryDetailRequestToken == requestToken
                    else { return }
                    self.openWaterfall?.mark("detail_failed", "error=\(error)")
                }
            }
            await MainActor.run { [weak self] in
                guard let self,
                      self.activeSessionId == sessionId,
                      self.primaryDetailRequestToken == requestToken
                else { return }
                let shouldFollowUp = self.primaryDetailRefreshPending
                self.primaryDetailRefreshPending = false
                self.primaryDetailTask = nil
                if shouldFollowUp,
                   !self.realtimePaused {
                    self.loadPrimaryDetail(api: api, sessionId: sessionId)
                }
            }
        }
    }

    /// Fetch worker metadata only after the first transcript snapshot is
    /// accepted. This is a secondary lane: it must never delay first paint,
    /// and refreshes coalesce so newer worker metadata is not lost.
    private func loadSubagents(api: SessionWorkspaceClient, sessionId: String) {
        guard activeSessionId == sessionId,
              !realtimePaused
        else { return }
        if subagentsTask != nil {
            subagentsRefreshPending = true
            return
        }
        subagentsRequestToken &+= 1
        let requestToken = subagentsRequestToken
        let generation = routeLoadGeneration
        openWaterfall?.mark("subagents_request_start")
        subagentsTask = Task { [weak self] in
            do {
                let response = try await api.sessionSubagents(id: sessionId)
                guard !Task.isCancelled else { return }
                await MainActor.run {
                    guard let self,
                          self.activeSessionId == sessionId,
                          self.routeLoadGeneration == generation,
                          self.subagentsRequestToken == requestToken,
                          !self.realtimePaused
                    else { return }
                    if self.subagents != response.children {
                        self.subagents = response.children
                        self.openWaterfall?.mark(
                            "subagents_loaded",
                            "count=\(response.children.count)"
                        )
                    } else {
                        self.openWaterfall?.mark("subagents_unchanged")
                    }
                }
            } catch is CancellationError {
                return
            } catch {
                await MainActor.run { [weak self] in
                    guard let self,
                          self.activeSessionId == sessionId,
                          self.subagentsRequestToken == requestToken
                    else { return }
                    self.openWaterfall?.mark("subagents_failed")
                }
            }
            await MainActor.run { [weak self] in
                guard let self,
                      self.subagentsRequestToken == requestToken
                else { return }
                self.subagentsTask = nil
                let shouldFollowUp = self.subagentsRefreshPending
                self.subagentsRefreshPending = false
                if shouldFollowUp,
                   self.activeSessionId == sessionId,
                   self.routeLoadGeneration == generation,
                   !self.realtimePaused {
                    self.loadSubagents(api: api, sessionId: sessionId)
                }
            }
        }
    }

    func stopConsoleWork(sessionId: String, appState: AppState) async {
        guard !isStoppingConsoleWork else { return }
        guard let api = apiFactory(appState.serverURL) else {
            stopErrorMessage = "The Longhouse server URL is invalid."
            return
        }
        isStoppingConsoleWork = true
        stopErrorMessage = nil
        defer { isStoppingConsoleWork = false }
        do {
            try await api.interruptConsoleTurn(id: sessionId)
            try? await refreshTail(api: api, sessionId: sessionId, allowFailure: true)
        } catch let LonghouseAPIError.structured(_, _, message) {
            stopErrorMessage = message.isEmpty ? "Could not stop background work." : message
        } catch {
            stopErrorMessage = "Could not stop background work. Refresh and try again."
        }
    }

    func prepareResume(sessionId: String, appState: AppState) async {
        guard !isPreparingResume else { return }
        guard let api = apiFactory(appState.serverURL) else {
            resumeErrorMessage = "The Longhouse server URL is invalid."
            return
        }
        isPreparingResume = true
        resumeErrorMessage = nil
        defer { isPreparingResume = false }
        do {
            resumeIntent = try await api.sessionResumeIntent(id: sessionId)
        } catch {
            resumeErrorMessage = "Could not prepare Resume. Refresh and try again."
        }
    }

    /// The server deduplicates a branch on its request id, so the id has to
    /// survive a retry of the same text: after a dropped response the first
    /// attempt may have succeeded, and a fresh id would start a second branch.
    /// Different text is a different request.
    private var branchAttempt: (text: String, id: String)?

    func startBranch(sessionId: String, appState: AppState) async {
        let text = branchMessage.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isBranching else { return }
        guard let api = apiFactory(appState.serverURL) else {
            branchErrorMessage = "The Longhouse server URL is invalid."
            return
        }
        if branchAttempt?.text != text {
            branchAttempt = (text: text, id: UUID().uuidString)
        }
        guard let attempt = branchAttempt else { return }
        isBranching = true
        branchErrorMessage = nil
        defer { isBranching = false }
        do {
            let branch = try await api.createSessionBranch(
                id: sessionId,
                message: text,
                clientRequestId: attempt.id
            )
            // Only clear the draft once the branch exists. Losing what someone
            // typed is the worst possible answer to a failure they can retry.
            branchAttempt = nil
            branchMessage = ""
            branchedSessionId = branch.sessionId
        } catch {
            branchErrorMessage = "Couldn't start the branch. Try again."
        }
    }

    func markBenchmarkSource(revision: Int, operation: String) {
        benchmarkSourceRevision = revision
        benchmarkSourceOperation = operation
    }

    func makeBugReportContext(sessionId: String, serverURL: String) -> Data {
        var stateFacts: [String: Any] = [
            "transcript_item_count": items.count,
            "recent_item_ids": Array(items.suffix(20).map(\.id)),
            "submitted_input_count": submittedInputs.count,
            "queued_input_count": queuedInputCount,
            "queued_elsewhere_count": queuedElsewhereCount,
            "failed_input_count": failedInputCount,
            "captured_at": ISO8601DateFormatter().string(from: Date()),
            "session_id": sessionId,
            "server_url": serverURL,
            "diagnostics": ClientDiagnosticsReporter.shared.snapshotEntries(sessionId: sessionId, limit: 100).map {
                var entry: [String: Any] = [
                    "at_ms": $0.at_ms,
                    "stage": $0.stage,
                ]
                if let detail = $0.detail { entry["detail"] = detail }
                if let entrySessionID = $0.session_id { entry["session_id"] = entrySessionID }
                return entry
            },
        ]
        if let activityState = detail?.stateFacts.activityState { stateFacts["activity_state"] = activityState }
        if let commitSeq = detail?.stateFacts.commitSeq { stateFacts["commit_seq"] = commitSeq }
        if let lastResultAt = detail?.stateFacts.lastResultAt { stateFacts["last_result_at"] = lastResultAt }
        if let displayTitle = detail?.displayTitle { stateFacts["display_title"] = displayTitle }
        if let transcriptReadThrough { stateFacts["transcript_read_through"] = transcriptReadThrough }
        if let appBuild = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String {
            stateFacts["app_build"] = appBuild
        }
        return (try? JSONSerialization.data(withJSONObject: stateFacts, options: [.sortedKeys])) ?? Data("{}".utf8)
    }


    func sendResult(
        text: String,
        sessionId: String,
        appState: AppState,
        intent: String = "auto",
        model: String? = nil,
        attachments: [ComposerAttachment] = [],
        replacingClientRequestId: String? = nil
    ) async -> SessionInputSendResult {
        let clientRequestId = "ios-\(UUID().uuidString)"
        let serverURL = TranscriptSnapshot.normalizedServerURL(appState.serverURL)
        let authGeneration = SharedAuthStore.authGeneration(for: serverURL)
        let normalizedModel: String? = {
            guard let model else { return nil }
            let value = model.trimmingCharacters(in: .whitespacesAndNewlines)
            return value.isEmpty ? nil : value
        }()
        let pending = PendingInputIntent(
            clientRequestId: clientRequestId,
            serverURL: serverURL,
            authGeneration: authGeneration,
            sessionId: sessionId,
            text: text,
            intent: intent,
            model: normalizedModel,
            attachments: attachments.map {
                PendingInputIntent.Attachment(
                    id: $0.id,
                    filename: $0.filename,
                    data: $0.data,
                    mimeType: $0.mimeType
                )
            },
            createdAt: Date()
        )
        // This synchronous atomic write is deliberately before even URL
        // construction. A process kill or transport failure can therefore
        // never lose the bytes needed to reconcile or explicitly retry.
        guard pendingInputStore.save(pending) else {
            return .persistenceFailed
        }
        if let replacingClientRequestId {
            if turnEndedDraft?.clientRequestId == replacingClientRequestId {
                turnEndedDraft = nil
            }
            // The replacement is durable before the old operation is removed.
            // A crash between these writes leaves both records recoverable.
            pendingInputStore.remove(
                serverURL: serverURL,
                sessionId: sessionId,
                authGeneration: authGeneration,
                clientRequestId: replacingClientRequestId
            )
            submittedInputs.removeAll { $0.clientRequestId == replacingClientRequestId }
        }
        submittedInputs.append(
            SubmittedInput(
                id: clientRequestId,
                clientRequestId: clientRequestId,
                text: text,
                intent: intent,
                attachmentSummaries: attachments.map(SubmittedInputAttachmentSummary.init),
                phase: .submitting,
                serverInputId: nil,
                lastError: nil,
                createdAt: pending.createdAt
            )
        )
        return await dispatchPendingInput(pending, sessionId: sessionId, appState: appState)
    }

    /// Explicit retry of an unconfirmed operation. It reloads the original
    /// bytes and keeps the same identity; no fresh UUID is ever allocated.
    func retryPendingInput(
        clientRequestId: String,
        sessionId: String,
        appState: AppState
    ) async -> Bool {
        guard !isSending else { return false }
        let serverURL = TranscriptSnapshot.normalizedServerURL(appState.serverURL)
        let authGeneration = SharedAuthStore.authGeneration(for: serverURL)
        guard let pending = pendingInputStore.load(
            serverURL: serverURL,
            sessionId: sessionId,
            authGeneration: authGeneration
        ).first(where: { $0.clientRequestId == clientRequestId && !$0.isDeliveryConfirmed })
        else {
            return false
        }

        let current = submittedInputs.first { $0.clientRequestId == clientRequestId }
        updateSubmittedInput(
            clientRequestId,
            phase: .submitting,
            serverInputId: current?.serverInputId,
            liveInputId: current?.liveInputId,
            turnId: current?.turnId,
            runId: current?.runId,
            deliveryStatus: current?.deliveryStatus,
            lastError: nil
        )
        lastSendOutcome = nil
        errorMessage = nil
        refreshErrorMessage = nil
        let result = await dispatchPendingInput(pending, sessionId: sessionId, appState: appState)
        return result.isSuccessfulHandoff

    }
    func pendingInput(
        clientRequestId: String,
        sessionId: String,
        appState: AppState
    ) -> PendingInputIntent? {
        let serverURL = TranscriptSnapshot.normalizedServerURL(appState.serverURL)
        let authGeneration = SharedAuthStore.authGeneration(for: serverURL)
        return pendingInputStore.load(
            serverURL: serverURL,
            sessionId: sessionId,
            authGeneration: authGeneration
        ).first { $0.clientRequestId == clientRequestId && !$0.isDeliveryConfirmed }
    }

    /// Discard is explicit: only this action releases retained bytes for a
    /// rejected/failed/cancelled operation.
    func discardPendingInput(
        clientRequestId: String,
        sessionId: String,
        appState: AppState
    ) {
        guard let pending = pendingInput(
            clientRequestId: clientRequestId,
            sessionId: sessionId,
            appState: appState
        ) else { return }
        pendingInputStore.remove(pending)
        submittedInputs.removeAll { $0.clientRequestId == clientRequestId }
    }


    private func restorePendingInputs(_ intents: [PendingInputIntent]) {
        submittedInputs.append(contentsOf: intents.map { intent in
            SubmittedInput(
                id: intent.clientRequestId,
                clientRequestId: intent.clientRequestId,
                text: intent.text,
                intent: intent.intent,
                attachmentSummaries: intent.displayAttachmentSummaries.map(SubmittedInputAttachmentSummary.init),
                phase: intent.isDeliveryConfirmed ? .sent : .couldNotConfirm,
                serverInputId: nil,
                lastError: intent.isDeliveryConfirmed ? nil : "Delivery status is not confirmed yet.",
                createdAt: intent.createdAt
            )
        })
    }

    private func reconcilePendingInputs(
        _ intents: [PendingInputIntent],
        sessionId: String,
        appState: AppState,
        authGeneration: String
    ) async {
        guard activeSessionId == sessionId,
              activeServerURL == TranscriptSnapshot.normalizedServerURL(appState.serverURL),
              SharedAuthStore.authGeneration(for: activeServerURL ?? "") == authGeneration,
              let api = apiFactory(appState.serverURL)
        else { return }
        for intent in intents {
            guard !Task.isCancelled else { return }
            do {
                guard let receipt = try await api.sessionInputReceipt(
                    id: sessionId,
                    clientRequestId: intent.clientRequestId
                ) else {
                    markInputUnconfirmedIfUnresolved(
                        intent.clientRequestId,
                        lastError: "Delivery status is not confirmed yet."
                    )
                    continue
                }
                if intent.isDeliveryConfirmed {
                    if receipt.eventId != nil {
                        pendingInputStore.remove(intent)
                        submittedInputs.removeAll { $0.clientRequestId == intent.clientRequestId }
                    } else {
                        updateSubmittedInput(
                            intent.clientRequestId,
                            phase: .sent,
                            serverInputId: receipt.inputId,
                            liveInputId: receipt.liveInputId,
                            turnId: receipt.turn?.turnId,
                            runId: receipt.turn?.runId,
                            deliveryStatus: receipt.deliveryStatus,
                            lastError: nil
                        )
                    }
                    continue
                }
                switch receipt.disposition {
                case .accepted:
                    let terminalStatus = receipt.deliveryStatus?.lowercased()
                    let turnState = receipt.turn?.state.lowercased()
                    // A delivered input whose Console turn was then stopped
                    // reached the provider: it is sent, not failed.
                    let stoppedAfterDelivery = terminalStatus == "delivered"
                        && (turnState == "cancelled" || turnState == "canceled")
                    let terminalSuccess: Bool = {
                        if stoppedAfterDelivery { return true }
                        if receipt.turn != nil {
                            return turnState == "completed"
                        }
                        return terminalStatus == "delivered" || terminalStatus == "sent"
                    }()
                    if terminalSuccess, receipt.eventId != nil {
                        pendingInputStore.remove(intent)
                        submittedInputs.removeAll { $0.clientRequestId == intent.clientRequestId }
                        continue
                    }
                    let ambiguousDelivery = isUncertainDeliveryError(receipt.error)
                    let cancelled = !stoppedAfterDelivery
                        && (terminalStatus == "cancelled" || turnState == "cancelled")
                    let terminalFailure = cancelled || (
                        !ambiguousDelivery
                            && (terminalStatus == "failed" || turnState == "failed")
                    )
                    let currentTurnIsFresh = receipt.turn?.isFresh == true
                    let queued = (turnState == "queued" && currentTurnIsFresh)
                        || (turnState == nil && terminalStatus == "queued")
                    let workingConsole =
                        currentTurnIsFresh && Self.workingConsoleTurnStates.contains(turnState ?? "")
                    let phase: SubmittedInputPhase = {
                        if terminalSuccess { return .sent }
                        if workingConsole { return .working }
                        if queued { return .queued }
                        if ambiguousDelivery { return .couldNotConfirm }
                        if terminalFailure { return .failed }
                        return .couldNotConfirm
                    }()
                    if terminalSuccess {
                        if receipt.eventId != nil {
                            pendingInputStore.remove(intent)
                        } else if !intent.isDeliveryConfirmed {
                            _ = pendingInputStore.save(intent.confirmedReceiptSummary())
                        }
                    }
                    let lastError: String?
                    if cancelled {
                        lastError = "This input was cancelled before completion."
                    } else if ambiguousDelivery || phase == .couldNotConfirm {
                        lastError = receipt.error ?? "Delivery status is not confirmed yet."
                    } else if terminalFailure {
                        lastError = receipt.error ?? "The turn did not complete."
                    } else {
                        lastError = nil
                    }
                    updateSubmittedInput(
                        intent.clientRequestId,
                        phase: phase,
                        serverInputId: receipt.inputId,
                        liveInputId: receipt.liveInputId,
                        turnId: receipt.turn?.turnId,
                        runId: receipt.turn?.runId,
                        deliveryStatus: receipt.deliveryStatus,
                        lastError: lastError
                    )
                case .rejected:
                    // Failed/cancelled payloads remain editable until the user
                    // replaces them with a newly persisted operation or discards.
                    updateSubmittedInput(
                        intent.clientRequestId,
                        phase: .failed,
                        serverInputId: receipt.inputId,
                        liveInputId: receipt.liveInputId,
                        turnId: receipt.turn?.turnId,
                        runId: receipt.turn?.runId,
                        deliveryStatus: receipt.deliveryStatus,
                        lastError: receipt.error ?? "The server rejected this input."
                    )
                case .couldNotConfirm:
                    markInputUnconfirmedIfUnresolved(
                        intent.clientRequestId,
                        serverInputId: receipt.inputId,
                        liveInputId: receipt.liveInputId,
                        turnId: receipt.turn?.turnId,
                        runId: receipt.turn?.runId,
                        deliveryStatus: receipt.deliveryStatus,
                        lastError: receipt.error ?? "Delivery status is not confirmed yet."
                    )
                }
            } catch {
                // A failed receipt fetch adds no evidence and cannot erase a
                // newer accepted turn.
                markInputUnconfirmedIfUnresolved(
                    intent.clientRequestId,
                    lastError: "Delivery status is not confirmed yet."
                )
            }
        }
    }


    private func markInputUnconfirmedIfUnresolved(
        _ clientRequestId: String,
        serverInputId: Int? = nil,
        liveInputId: String? = nil,
        turnId: String? = nil,
        runId: String? = nil,
        deliveryStatus: String? = nil,
        lastError: String
    ) {
        // Missing or ambiguous receipt reads must not downgrade a newer result.
        guard let current = submittedInputs.first(where: {
            $0.clientRequestId == clientRequestId
        }) else { return }
        guard current.phase == .submitting || current.phase == .couldNotConfirm else { return }
        updateSubmittedInput(
            clientRequestId,
            phase: .couldNotConfirm,
            serverInputId: serverInputId ?? current.serverInputId,
            liveInputId: liveInputId ?? current.liveInputId,
            turnId: turnId ?? current.turnId,
            runId: runId ?? current.runId,
            deliveryStatus: deliveryStatus ?? current.deliveryStatus,
            lastError: lastError
        )
    }

    /// Automatic retries for transport failures, draining responses and K1
    /// refusals reuse the original request ID so server idempotency bounds
    /// duplicate delivery across restarts.
    private static let automaticResendDelays: [Duration] = [
        .seconds(1), .seconds(2), .seconds(3), .seconds(5), .seconds(8),
        .seconds(10), .seconds(10), .seconds(15), .seconds(15), .seconds(20),
    ]
    static let reconnectingDetail = "reconnecting to Longhouse"

    private func canRetrySameRequest(_ error: Error) -> Bool {
        switch error {
        case let apiError as LonghouseAPIError:
            switch apiError {
            case .structured(_, _, _):
                return apiError.isRuntimeDraining
            case .runtimeRestarting:
                return true
            case .upstreamFailed, .serviceUnavailable:
                return true
            default:
                return false
            }
        case let urlError as URLError:
            return [.notConnectedToInternet, .networkConnectionLost, .timedOut,
                    .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed].contains(urlError.code)
        default:
            return false
        }
    }

    private func scheduleAutomaticResend(
        _ pending: PendingInputIntent,
        sessionId: String,
        appState: AppState,
        attempt: Int,
        retryAfter: Int? = nil
    ) -> Bool {
        guard attempt < Self.automaticResendDelays.count else { return false }
        let delay = max(Self.automaticResendDelays[attempt], .seconds(Int64(retryAfter ?? 0)))
        updateSubmittedInput(
            pending.clientRequestId,
            phase: .submitting,
            serverInputId: nil,
            lastError: Self.reconnectingDetail
        )
        Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard let self,
                  let phase = self.submittedInputs.first(where: {
                      $0.clientRequestId == pending.clientRequestId
                  })?.phase,
                  phase == .submitting || phase == .couldNotConfirm,
                  let current = self.pendingInput(
                      clientRequestId: pending.clientRequestId,
                      sessionId: sessionId,
                      appState: appState
                  )
            else { return }
            _ = await self.dispatchPendingInput(
                current,
                sessionId: sessionId,
                appState: appState,
                automaticResendAttempt: attempt + 1
            )
        }
        return true
    }

    private func applyHostLifecycle(_ lifecycle: HostLifecycle, sessionId: String) {
        guard activeSessionId == sessionId, !realtimePaused else { return }
        if lifecycle.state == .serving {
            observeHostServingEvidence(sessionId: sessionId)
            return
        }
        let now = Date()
        var next = hostUpdateState
        next.apply(lifecycle, now: now)
        setHostUpdateState(next, at: now)
    }

    private func observeRuntimeRestarting(_ claim: HostLifecycle?) {
        let now = Date()
        var next = hostUpdateState
        next.observeRuntimeRestarting(claim: claim, now: now)
        setHostUpdateState(next, at: now)
    }

    private func observeHostServingEvidence(sessionId: String) {
        guard activeSessionId == sessionId, !realtimePaused else { return }
        var next = hostUpdateState
        next.observeServingEvidence()
        setHostUpdateState(next, at: Date())
    }

    private func setHostUpdateState(_ next: HostUpdateState, at now: Date) {
        let wasActive = hostUpdateState.isActive(at: hostUpdateNow)
        let isActive = next.isActive(at: now)
        hostUpdateState = next
        hostUpdateNow = now
        if wasActive != isActive { transcriptRevision &+= 1 }
    }

    func tickHostUpdateClock(at now: Date) {
        guard hostUpdateState.claimStartedAt != nil else { return }
        let wasActive = hostUpdateState.isActive(at: hostUpdateNow)
        hostUpdateNow = now
        if wasActive != hostUpdateState.isActive(at: now) { transcriptRevision &+= 1 }
    }

    func submittedInputsForTranscript(at now: Date) -> [SubmittedInput] {
        guard hostUpdateState.isActive(at: now) else { return submittedInputs }
        return submittedInputs.map { input in
            guard input.phase == .submitting || input.phase == .couldNotConfirm else { return input }
            var queuedInput = input
            queuedInput.lastError = HostLinkCopy.sendQueued
            return queuedInput
        }
    }

    private func dispatchPendingInput(
        _ pending: PendingInputIntent,
        sessionId: String,
        appState: AppState,
        automaticResendAttempt: Int = 0
    ) async -> SessionInputSendResult {
        guard let api = apiFactory(appState.serverURL) else {
            updateSubmittedInput(
                pending.clientRequestId,
                phase: .couldNotConfirm,
                serverInputId: nil,
                lastError: "The Longhouse server URL is invalid."
            )
            errorMessage = "Could not confirm delivery. Check the server URL and retry with the same request."
            return .unknown
        }
        isSending = true
        defer { isSending = false }
        do {
            let response: SessionInputResponse
            if pending.attachments.isEmpty {
                response = try await api.sendInput(
                    id: sessionId,
                    text: pending.text,
                    intent: pending.intent,
                    clientRequestId: pending.clientRequestId,
                    model: pending.model
                )
            } else {
                response = try await api.sendInputMultipart(
                    id: sessionId,
                    text: pending.text,
                    intent: pending.intent,
                    attachments: pending.composerAttachments(),
                    clientRequestId: pending.clientRequestId,
                    model: pending.model
                )
            }
            guard response.clientRequestId == nil
                || response.clientRequestId == pending.clientRequestId
            else {
                throw LonghouseAPIError.unexpectedResponse(
                    "Longhouse returned a different operation identity."
                )
            }
            sendCounter &+= 1
            let turnState = response.turn?.state.lowercased()
            lastSendOutcome = {
                guard response.disposition == .accepted else { return nil }
                guard let turnState else { return response.outcome }
                switch turnState {
                case "queued":
                    return response.turn?.isFresh == true ? .queued : nil
                case "completed":
                    return .sent
                case "starting", "active", "draining", "failed", "cancelled", "canceled":
                    return nil
                default:
                    return response.outcome
                }
            }()
            let ownClientRequestIds = Set(submittedInputs.map(\.clientRequestId))
            queuedElsewhereCount = response.queuedElsewhereCount(excluding: ownClientRequestIds)
            queuedInputCount = Self.queuedLineTotal(
                (response.pendingInputCount, queuedElsewhereCount),
                isConsole: detail?.stateFacts.mode == "console"
            )
            failedInputCount = response.visibleFailedInputCount(ownClientRequestIds: ownClientRequestIds)
            switch response.disposition {
            case .unknown:
                updateSubmittedInput(
                    pending.clientRequestId,
                    phase: .couldNotConfirm,
                    serverInputId: response.inputId,
                    liveInputId: response.liveInputId,
                    turnId: response.turn?.turnId,
                    runId: response.turn?.runId,
                    deliveryStatus: response.deliveryStatus,
                    lastError: "Delivery status is not confirmed yet."
                )
                refreshErrorMessage = "Delivery status is not confirmed yet."
                return .unknown
            case .rejected:
                updateSubmittedInput(
                    pending.clientRequestId,
                    phase: .failed,
                    serverInputId: response.inputId,
                    liveInputId: response.liveInputId,
                    turnId: response.turn?.turnId,
                    runId: response.turn?.runId,
                    deliveryStatus: response.deliveryStatus,
                    lastError: "The server rejected this input."
                )
                return .rejected
            case .accepted:
                let acceptedTurnState = response.turn?.state.lowercased()
                let terminalFailure = acceptedTurnState.map {
                    ["failed", "cancelled", "canceled"].contains($0)
                } == true || (response.turn == nil && response.deliveryStatus.map {
                    ["failed", "cancelled", "canceled"].contains($0)
                } == true)
                let helmDelivered = response.turn == nil
                    && (
                        response.outcome == .sent
                        || response.deliveryStatus?.lowercased() == "delivered"
                        || response.deliveryStatus?.lowercased() == "sent"
                    )
                let terminalSuccess = helmDelivered || acceptedTurnState == "completed"
                let currentTurnIsFresh = response.turn?.isFresh == true
                let workingConsole =
                    currentTurnIsFresh && Self.workingConsoleTurnStates.contains(acceptedTurnState ?? "")
                let queued = (acceptedTurnState == "queued" && currentTurnIsFresh)
                    || (
                        acceptedTurnState == nil
                            && (
                                response.outcome == .queued
                                || response.deliveryStatus?.lowercased() == "queued"
                            )
                    )
                let phase: SubmittedInputPhase = {
                    if terminalFailure { return .failed }
                    if terminalSuccess { return .sent }
                    if workingConsole { return .working }
                    if queued { return .queued }
                    return .couldNotConfirm
                }()
                if terminalSuccess {
                    _ = pendingInputStore.save(pending.confirmedReceiptSummary())
                }
                updateSubmittedInput(
                    pending.clientRequestId,
                    phase: phase,
                    serverInputId: response.inputId,
                    liveInputId: response.liveInputId,
                    turnId: response.turn?.turnId,
                    runId: response.turn?.runId,
                    deliveryStatus: response.deliveryStatus,
                    lastError: terminalFailure
                        ? "The turn did not complete."
                        : (phase == .couldNotConfirm ? "Delivery status is not confirmed yet." : nil)
                )
            }
            Task { [weak self] in
                guard let self else { return }
                try? await self.refreshTail(api: api, sessionId: sessionId, allowFailure: true)
            }
            observeHostServingEvidence(sessionId: sessionId)
            switch response.disposition {
            case .accepted:
                if let turnState {
                    return turnState == "queued" && response.turn?.isFresh == true ? .queued : .accepted
                }
                return response.outcome == .queued ? .queued : .accepted
            case .rejected:
                return .rejected
            case .unknown:
                return .unknown
            }
        } catch let inputError as SessionInputOperationError {
            if pending.intent == "steer", inputError.errorCode == "turn_ended" {
                let reason = inputError.message.isEmpty
                    ? "Active turn ended before your update arrived."
                    : inputError.message
                updateSubmittedInput(
                    pending.clientRequestId,
                    phase: .needsUserDecision,
                    serverInputId: inputError.inputId,
                    liveInputId: inputError.liveInputId,
                    turnId: inputError.turn?.turnId,
                    runId: inputError.turn?.runId,
                    deliveryStatus: inputError.deliveryStatus,
                    lastError: reason
                )
                turnEndedDraft = TurnEndedInput(
                    clientRequestId: pending.clientRequestId,
                    text: pending.text
                )
                errorMessage = nil
                return .rejected
            }
            if inputError.errorCode?.lowercased() == "runtime_draining",
               inputError.inputId == nil, inputError.liveInputId == nil,
               scheduleAutomaticResend(pending, sessionId: sessionId, appState: appState, attempt: automaticResendAttempt) {
                errorMessage = nil
                return .unknown
            }
            let ambiguousDelivery = isUncertainDeliveryError(inputError.errorCode)
            let cancelled = ["cancelled", "canceled"].contains(
                inputError.deliveryStatus?.lowercased() ?? ""
            )
            let terminalFailure = cancelled || (
                !ambiguousDelivery
                    && inputError.deliveryStatus?.lowercased() == "failed"
            )
            let phase: SubmittedInputPhase = ambiguousDelivery && !cancelled
                ? .couldNotConfirm
                : (inputError.disposition == .rejected || terminalFailure
                    ? .failed
                    : .couldNotConfirm)
            updateSubmittedInput(
                pending.clientRequestId,
                phase: phase,
                serverInputId: inputError.inputId,
                liveInputId: inputError.liveInputId,
                turnId: inputError.turn?.turnId,
                runId: inputError.turn?.runId,
                deliveryStatus: inputError.deliveryStatus,
                lastError: inputError.message
            )
            if inputError.disposition == .unknown || ambiguousDelivery {
                refreshErrorMessage = inputError.message
                return .unknown
            }
            if inputError.disposition == .accepted {
                return .accepted
            }
            errorMessage = "Could not send: \(inputError.message)"
            return .rejected
        } catch {
            let runtimeRetryAfter: Int?
            if let runtimeError = error as? LonghouseAPIError,
               case .runtimeRestarting(let claim, let retryAfter) = runtimeError {
                observeRuntimeRestarting(claim)
                runtimeRetryAfter = retryAfter
            } else {
                runtimeRetryAfter = nil
            }
            let failureMessage = sendFailureMessage(for: error)
            if canRetrySameRequest(error),
               scheduleAutomaticResend(
                   pending,
                   sessionId: sessionId,
                   appState: appState,
                   attempt: automaticResendAttempt,
                   retryAfter: runtimeRetryAfter
               ) {
                errorMessage = nil
                return .unknown
            }
            if runtimeRetryAfter != nil {
                updateSubmittedInput(
                    pending.clientRequestId,
                    phase: .couldNotConfirm,
                    serverInputId: nil,
                    lastError: HostLinkCopy.sendQueued
                )
                errorMessage = nil
                refreshErrorMessage = nil
                return .unknown
            }
            if sendConfirmationMayHaveLanded(error) {
                updateSubmittedInput(
                    pending.clientRequestId,
                    phase: .couldNotConfirm,
                    serverInputId: nil,
                    lastError: failureMessage
                )
                errorMessage = nil
                refreshErrorMessage = failureMessage
                Task { [weak self] in
                    guard let self else { return }
                    let normalizedServerURL = TranscriptSnapshot.normalizedServerURL(appState.serverURL)
                    let authGeneration = SharedAuthStore.authGeneration(for: normalizedServerURL)
                    await self.reconcilePendingInputs(
                        [pending],
                        sessionId: sessionId,
                        appState: appState,
                        authGeneration: authGeneration
                    )
                    try? await self.refreshTail(api: api, sessionId: sessionId, allowFailure: true)
                }
                return .unknown
            }
            // A definitive rejection still retains its complete payload until
            // the user edits it into a newly persisted operation or discards it.
            updateSubmittedInput(
                pending.clientRequestId,
                phase: .failed,
                serverInputId: nil,
                lastError: failureMessage
            )
            errorMessage = "Could not send: \(failureMessage)"
            Task { [weak self] in
                guard let self else { return }
                try? await self.refreshTail(api: api, sessionId: sessionId, allowFailure: true)
            }
            return .rejected
        }
    }

    /// Replaces the exact rejected steer only after the queued operation has
    /// been durably persisted with a new request ID.
    func queueInsteadOfSteer(
        clientRequestId: String,
        sessionId: String,
        appState: AppState
    ) async -> Bool {
        guard let decision = turnEndedDraft,
              decision.clientRequestId == clientRequestId,
              submittedInputs.contains(where: {
                  $0.clientRequestId == clientRequestId && $0.phase == .needsUserDecision
              }),
              let pending = pendingInput(
                  clientRequestId: clientRequestId,
                  sessionId: sessionId,
                  appState: appState
              ),
              pending.intent == "steer"
        else { return false }

        let result = await sendResult(
            text: pending.text,
            sessionId: sessionId,
            appState: appState,
            intent: "queue",
            model: pending.model,
            attachments: pending.composerAttachments(),
            replacingClientRequestId: clientRequestId
        )
        return result.isSuccessfulHandoff
    }
    /// Read-on-open acknowledgement for Console results
    /// (console-unread-acknowledgement spec): acknowledge exactly the result
    /// this client rendered. Fire-and-forget; the server is a max-write no-op
    /// when already read.
    static func unreadReadThrough(
        facts: SessionStateFacts?,
        sceneIsActive: Bool,
        transcriptFrameReady: Bool = true,
        renderedReadThrough: String? = nil
    ) -> String? {
        guard transcriptFrameReady,
              sceneIsActive,
              let facts,
              facts.unread,
              let renderedReadThrough
        else { return nil }
        return renderedReadThrough
    }

    func acknowledgeUnreadIfNeeded(
        sessionId: String,
        appState: AppState,
        sceneIsActive: Bool
    ) async {
        guard let readThrough = Self.unreadReadThrough(
            facts: detail?.stateFacts,
            sceneIsActive: sceneIsActive,
            transcriptFrameReady: isTranscriptFrameReady,
            renderedReadThrough: renderedTranscriptReadThrough
        ) else { return }
        guard let api = apiFactory(appState.serverURL) else { return }
        try? await api.markSessionRead(id: sessionId, readThrough: readThrough)
    }

    func respondToPauseRequest(
        sessionId: String,
        appState: AppState,
        pauseRequest: SessionPauseRequest,
        decision: String,
        answers: [String: [String]]?,
        content: String?,
        message: String?
    ) async -> Bool {
        guard let api = apiFactory(appState.serverURL) else {
            pauseResponseErrorMessage = "Invalid server URL"
            return false
        }
        isRespondingToPauseRequest = true
        pauseResponseErrorMessage = nil
        defer { isRespondingToPauseRequest = false }
        do {
            _ = try await api.respondToPauseRequest(
                sessionId: sessionId,
                pauseRequestId: pauseRequest.id,
                decision: decision,
                answers: answers,
                content: content,
                message: message
            )
            try? await refreshTail(api: api, sessionId: sessionId, allowFailure: true)
            return true
        } catch let LonghouseAPIError.structured(_, _, message) {
            pauseResponseErrorMessage = message.isEmpty ? "Failed to send answer." : message
            try? await refreshTail(api: api, sessionId: sessionId, allowFailure: true)
            return false
        } catch {
            pauseResponseErrorMessage = "Answer failed: \(error.localizedDescription)"
            try? await refreshTail(api: api, sessionId: sessionId, allowFailure: true)
            return false
        }
    }

    func recordTranscriptDiagnostics(
        _ diagnostics: RenderBeaconReporter.WebKitDiagnostics,
        sessionId: String,
        appState: AppState
    ) async {
        transcriptDiagnostics = diagnostics
        let renderMs = diagnostics.render_duration_ms.map { " render_ms=\($0)" } ?? ""
        openWaterfall?.mark(
            "webkit_\(diagnostics.stage)",
            "rows=\(diagnostics.row_count) bytes=\(diagnostics.payload_byte_size)\(renderMs)"
                + " latest=\(diagnostics.latest_item_id ?? "none") revision=\(diagnostics.source_revision ?? -1)"
                + " sequence=\(diagnostics.render_sequence) js_failures=\(diagnostics.js_failure_count)"
        )
        guard diagnostics.stage == "rendered" || diagnostics.stage == "failed" else { return }
        guard let api = apiFactory(appState.serverURL) else { return }
        await reportStateRenderBeacon(
            api: api,
            sessionId: sessionId,
            webkitDiagnostics: diagnostics
        )
        await reportRenderBeacon(
            api: api,
            sessionId: sessionId,
            events: lastWorkspaceEvents,
            webkitDiagnostics: diagnostics
        )
    }

    func recordStateRenderBeacon(sessionId: String, appState: AppState) async {
        guard let api = apiFactory(appState.serverURL) else { return }
        await reportStateRenderBeacon(
            api: api,
            sessionId: sessionId,
            webkitDiagnostics: nil
        )
    }
    /// Tail/detail requests can finish out of order. A transcript watermark
    /// must never move backward just because an older tail carried older
    /// metadata; keep the greatest observed result timestamp.
    private func retainedTranscriptReadThrough(_ incoming: String?) -> String? {
        guard let incoming else { return transcriptReadThrough }
        guard let current = transcriptReadThrough else { return incoming }
        guard let incomingDate = LonghouseDateParser.parse(incoming),
              let currentDate = LonghouseDateParser.parse(current)
        else {
            return incoming >= current ? incoming : current
        }
        return incomingDate >= currentDate ? incoming : current
    }

    /// A provider result timestamp is acknowledgement evidence only when the
    /// same accepted workspace says its archive is current. During lagging or
    /// unknown convergence the state facts may be newer than the rows in this
    /// page; retaining the previous boundary is safer than falsely marking the
    /// unseen result read.
    private func acceptedTranscriptReadThrough(for session: SessionDetail) -> String? {
        guard session.stateFacts.transcriptConvergence == "current" else {
            return transcriptReadThrough
        }
        return retainedTranscriptReadThrough(session.stateFacts.lastResultAt)
    }


    private func markTranscriptNeedsReadThrough(_ readThrough: String?, force: Bool = false) {
        transcriptReadThrough = readThrough
        pendingTranscriptReadThrough = readThrough
        guard force || renderedTranscriptReadThrough != readThrough else { return }
        renderedTranscriptReadThrough = nil
    }

    func prepareTranscriptRetry() {
        guard hasLoadedTranscript || !items.isEmpty || !submittedInputs.isEmpty else { return }
        markTranscriptNeedsReadThrough(
            transcriptReadThrough ?? pendingTranscriptReadThrough ?? renderedTranscriptReadThrough,
            force: true
        )
        isTranscriptFrameReady = false
        transcriptRendererErrorMessage = nil
        transcriptRenderRetryRevision &+= 1
    }

    /// A frame receipt is scoped to this route/document, but a first valid
    /// frame does not need to be the newest payload. The encoder can finish
    /// payload A while payload B waits behind it; A is still usable UI.
    /// Strict identity remains required for the rendered watermark so unread
    /// acknowledgement never outruns the rows the user actually saw.
    func recordTranscriptFrameRendered(_ receipt: WebTranscriptRenderReceipt) {
        let isRouteReceipt =
            receipt.contentRevision >= transcriptRevisionFloor
                && receipt.contentRevision <= transcriptRevision
                && receipt.retryRevision == transcriptRenderRetryRevision
        guard isRouteReceipt else {
            openWaterfall?.mark(
                "transcript_frame_stale",
                "receipt_revision=\(receipt.contentRevision) wanted=\(transcriptRevision) floor=\(transcriptRevisionFloor)"
            )
            return
        }
        let isCurrentReceipt =
            receipt.contentRevision == transcriptRevision
                && receipt.transcriptReadThrough == transcriptReadThrough
        let wasReady = isTranscriptFrameReady
        if !isCurrentReceipt {
            // A stale payload can still be the first usable pixels for this
            // document, but it must not resurrect a retry surface raised by
            // the current payload.
            guard transcriptRendererErrorMessage == nil else {
                openWaterfall?.mark(
                    "transcript_frame_visible_discarded",
                    "receipt_revision=\(receipt.contentRevision) wanted=\(transcriptRevision)"
                )
                return
            }
            if !wasReady {
                isTranscriptFrameReady = true
                finishFirstTranscriptFrame()
            }
            // Do not advance the unread watermark: these pixels may not
            // include the newest accepted transcript boundary.
            openWaterfall?.mark(
                "transcript_frame_visible",
                "receipt_revision=\(receipt.contentRevision) wanted=\(transcriptRevision)"
            )
            return
        }

        if !wasReady {
            isTranscriptFrameReady = true
            transcriptRendererErrorMessage = nil
        }
        renderedTranscriptReadThrough = receipt.transcriptReadThrough
        pendingTranscriptReadThrough = nil
        guard !wasReady else { return }
        finishFirstTranscriptFrame()
    }

    private func finishFirstTranscriptFrame() {
        guard
            let sessionId = activeSessionId,
            let serverURL = activeServerURL,
            let api = apiFactory(serverURL)
        else { return }
        if historyFillPendingFirstFrame {
            historyFillPendingFirstFrame = false
            Task { [weak self] in
                await self?.fillHistoryForShortViewport(api: api, sessionId: sessionId)
            }
        } else {
            scheduleOlderPrefetch(api: api, sessionId: sessionId)
        }
    }

    func recordTranscriptFrameFailed(_ receipt: WebTranscriptRenderReceipt) {
        let isRouteReceipt =
            receipt.contentRevision >= transcriptRevisionFloor
                && receipt.contentRevision <= transcriptRevision
                && receipt.retryRevision == transcriptRenderRetryRevision
        guard
            activeSessionId != nil,
            isRouteReceipt,
            receipt.contentRevision == transcriptRevision,
            receipt.transcriptReadThrough == transcriptReadThrough
        else {
            openWaterfall?.mark(
                "transcript_frame_failure_stale",
                "receipt_revision=\(receipt.contentRevision) wanted=\(transcriptRevision) floor=\(transcriptRevisionFloor)"
            )
            return
        }
        isTranscriptFrameReady = false
        markTranscriptNeedsReadThrough(transcriptReadThrough, force: true)
        transcriptRendererErrorMessage = "Transcript rendering was interrupted."
    }


    func recordTranscriptLifecycle(_ stage: String) {
        openWaterfall?.mark(stage)
        switch stage {
        case "transcript_frame_rendered":
            // Readiness and watermark publication are owned by the
            // revision-scoped receipt, not this unqualified lifecycle string.
            break
        case "transcript_frame_failed":
            // The typed receipt owns failure visibility and rejects stale
            // attempts. This lifecycle mark is timing-only.
            break
        case "webview_document_failed":
            isTranscriptFrameReady = false
            markTranscriptNeedsReadThrough(
                transcriptReadThrough ?? pendingTranscriptReadThrough ?? renderedTranscriptReadThrough,
                force: true
            )
            transcriptRendererErrorMessage = "Transcript document could not be loaded."
        case "webview_content_process_terminated":
            isTranscriptFrameReady = false
            markTranscriptNeedsReadThrough(
                transcriptReadThrough ?? pendingTranscriptReadThrough ?? renderedTranscriptReadThrough,
                force: true
            )
            transcriptRendererErrorMessage = nil
        default:
            break
        }
    }
    /// transcript's first ready frame. Keeping this outside the lifecycle
    /// mutation lets the history prefetch arm without another main-actor task
    /// competing for the same opening turn.
    func transcriptFrameDidBecomeReady(sessionId: String, appState: AppState) {
        guard isTranscriptFrameReady,
              let api = apiFactory(appState.serverURL) else { return }
        loadSubagents(api: api, sessionId: sessionId)
    }

    private func startVisiblePolling(sessionId: String, appState: AppState) {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            var ticks = 0
            while !Task.isCancelled {
                guard let self = self else { break }
                let pendingPollDelay = await MainActor.run {
                    Self.pendingInputPollDelay(submittedInputs: self.submittedInputs, now: Date())
                }
                let delay = pendingPollDelay ?? Self.visiblePollDelayNanoseconds(completedTicks: ticks)
                try? await Task.sleep(nanoseconds: delay)
                if Task.isCancelled { break }
                ticks += 1
                let (connected, hasRunningTool, setupPending, stillHasPendingInput, activityStale) = await MainActor.run {
                    let now = Date()
                    return (
                        self.streamConnected,
                        self.lastWorkspaceEvents.contains { $0.toolCallState == .running },
                        self.detail?.canDraftBeforeSendReady == true,
                        Self.pendingInputPollDelay(submittedInputs: self.submittedInputs, now: now) != nil,
                        self.heldActivityEvidenceIsStale(asOf: now)
                    )
                }
                let managed = await MainActor.run {
                    if self.detail?.canDraftBeforeSendReady == true { return true }
                    guard let facts = self.detail?.stateFacts else { return false }
                    return facts.controlOwnership == "owned"
                        || facts.mode == "console"
                }
                // Polling is a correctness fallback, not a second live lane.
                // A healthy stream applies provisional transcript patches
                // directly and later emits a durable revision wake. Polling
                // while that stream is healthy amplifies every pending turn
                // into repeated full-tail rebuilds.
                if Self.shouldPollVisibleSession(
                    connected: connected,
                    hasRunningTool: hasRunningTool,
                    managed: managed,
                    setupPending: setupPending,
                    pendingInput: stillHasPendingInput,
                    activityStale: activityStale,
                    ticks: ticks
                ) {
                    self.openWaterfall?.mark(
                        "poll_tail",
                        "connected=\(connected) setup_pending=\(setupPending) pending_input=\(stillHasPendingInput) running_tool=\(hasRunningTool) activity_stale=\(activityStale) tick=\(ticks)"
                    )
                    await self.pollTick(sessionId: sessionId, appState: appState)
                }
            }
        }
    }

    static func shouldPollVisibleSession(
        connected: Bool,
        hasRunningTool: Bool,
        managed: Bool,
        setupPending: Bool = false,
        pendingInput: Bool = false,
        activityStale: Bool = false,
        ticks: Int
    ) -> Bool {
        if ticks <= 3 { return !connected }
        if pendingInput {
            // A parsed SSE handshake proves the stream was live once, not that
            // every later frame reaches the phone. Keep one low-frequency
            // correctness fetch while an optimistic send is unresolved so a
            // buffered or silently stalled stream cannot leave "Working..."
            // on screen forever. Disconnected streams retain the faster path.
            return !connected || ticks.isMultiple(of: 4)
        }
        // Launch-state changes arrive on the workspace stream. Polling the
        // entire mobile tail while that stream is healthy turned every new
        // Console launch into a 750ms request/build/WebKit-render loop.
        if setupPending { return !connected }
        if !connected { return true }
        // An activity window is decided by the reader's clock, not by a
        // provider frame, so a healthy stream can hold a claim nothing will
        // ever correct: a wedged turn ships nothing further, and the compact
        // detail lane refuses a response that carries no newer catalog commit.
        // One slow fetch replaces the client's own guess with the server's
        // verdict ("Last observed thinking").
        if activityStale { return ticks.isMultiple(of: 6) }
        if hasRunningTool, ticks % 12 == 0 { return true }
        _ = managed
        return false
    }

    /// Is this viewer still rendering provider work from a window that has
    /// passed? Mirrors `activityClaimIsStale` in `web/src/shared/session/activityEvidence.ts`.
    ///
    /// It asks about states that make a *claim* -- work in flight, or an
    /// explicit stall -- so the server's re-mint (which serves `unknown` with
    /// the same past window) clears the predicate and the poll stops.
    private func heldActivityEvidenceIsStale(asOf now: Date) -> Bool {
        guard let facts = detail?.stateFacts else { return false }
        let claimsWork =
            facts.activityState == "thinking"
            || facts.activityState == "executing"
            || facts.activityState == "stalled"
        return claimsWork && !facts.activityEvidenceIsLive(asOf: now)
    }

    static func visiblePollDelayNanoseconds(completedTicks: Int) -> UInt64 {
        completedTicks < 3 ? 750_000_000 : 5_000_000_000
    }

    static func pendingInputPollDelay(submittedInputs: [SubmittedInput], now: Date) -> UInt64? {
        let activePhases: Set<SubmittedInputPhase> = [.submitting, .queued, .working]
        if submittedInputs.contains(where: {
            activePhases.contains($0.phase) && $0.turnId != nil
        }) {
            // Console turn state is authoritative by ID, including queued
            // turns that have outlived the recent-list window.
            return 2_000_000_000
        }
        let activeAges = submittedInputs.compactMap { input -> TimeInterval? in
            guard activePhases.contains(input.phase) else { return nil }
            return max(0, now.timeIntervalSince(input.createdAt))
        }
        guard let youngest = activeAges.min() else { return nil }
        let hasQueuedInput = submittedInputs.contains { $0.phase == .queued }
        guard hasQueuedInput || youngest <= 120 else { return nil }
        if youngest <= 15 { return 750_000_000 }
        if youngest <= 45 { return 2_000_000_000 }
        return 5_000_000_000
    }

    private func startStream(sessionId: String, appState: AppState) {
        // A prior stream actor may still own a URLSession + draining task.
        // Stop it before replacing the reference — otherwise it leaks until
        // timeoutIntervalForResource (1h) expires on its own.
        streamTask?.cancel()
        if let prior = stream {
            Task { await prior.stop() }
        }
        streamConnected = false
        realtimeConnection = .connecting
        guard let base = URL(string: appState.serverURL) else { return }
        // A sequence without its runtime epoch is not comparable to the
        // current process. Old snapshots may predate epoch persistence, so
        // deliberately start cold and let the durable tail provide truth.
        let resumeSeq = streamEpoch == nil ? nil : lastPubsubSeq
        openWaterfall?.mark(
            "stream_start",
            "since_seq=\(resumeSeq ?? 0) known_epoch=\(streamEpoch != nil) known_fingerprint=\(lastWorkspaceRevisionFingerprint != nil)"
        )
        let s = streamFactory(base, sessionId, resumeSeq, lastWorkspaceRevisionFingerprint)
        stream = s
        streamTask = Task { [weak self] in
            await s.setStreamEpoch(self?.streamEpoch)
            let events = await s.start()
            for await event in events {
                if Task.isCancelled { break }
                await self?.handleStreamEvent(event, sessionId: sessionId, appState: appState)
            }
        }
    }

    private func handleStreamEvent(_ event: SessionWorkspaceStream.Event, sessionId: String, appState: AppState) async {
        switch event {
        case .connected(let connected):
            let epochChanged = connected.stream_epoch != nil
                && streamEpoch != nil
                && connected.stream_epoch != streamEpoch
            if let epoch = connected.stream_epoch {
                streamEpoch = epoch
            }
            streamConnected = true
            if connected.admission == .open {
                observeHostServingEvidence(sessionId: sessionId)
            }
            realtimeConnection = .connected
            streamAuthRefreshAttempted = false
            openWaterfall?.mark(
                "stream_connected",
                "epoch=\(connected.stream_epoch ?? "unknown")"
            )
            if epochChanged {
                // A restarted process can reuse sequence values. Discard the
                // old cursor/fingerprint and converge from the durable tail.
                lastPubsubSeq = nil
                lastWorkspaceRevisionFingerprint = nil
                if let api = apiFactory(appState.serverURL) {
                    requestRealtimeRefresh(api: api, sessionId: sessionId)
                }
            }
            let normalizedServerURL = TranscriptSnapshot.normalizedServerURL(appState.serverURL)
            let authGeneration = SharedAuthStore.authGeneration(for: normalizedServerURL)
            let pending = pendingInputStore.load(
                serverURL: normalizedServerURL,
                sessionId: sessionId,
                authGeneration: authGeneration
            )
            if !pending.isEmpty {
                await reconcilePendingInputs(
                    pending,
                    sessionId: sessionId,
                    appState: appState,
                    authGeneration: authGeneration
                )
            }
        case .hostLifecycle(let lifecycle):
            applyHostLifecycle(lifecycle, sessionId: sessionId)
        case .disconnected(let error):
            streamConnected = false
            realtimeConnection = .disconnected
            openWaterfall?.mark("stream_disconnected", "error=\(error?.localizedDescription ?? "none")")
        case .decodeFailed(let detail):
            // The cursor is already past the frame; only durable state can
            // recover whatever it carried.
            openWaterfall?.mark("stream_decode_failed", detail)
            guard let api = apiFactory(appState.serverURL) else { return }
            requestRealtimeRefresh(api: api, sessionId: sessionId)
        case .diagnostic(let stage, let detail):
            openWaterfall?.mark(stage, detail)
        case .unauthorized:
            streamConnected = false
            realtimeConnection = .disconnected
            openWaterfall?.mark("stream_unauthorized")
            await handleStreamUnauthorized(sessionId: sessionId, appState: appState)
        case .replayGap(let gap):
            streamConnected = true
            realtimeConnection = .connected
            let epochChanged = gap.stream_epoch != nil
                && streamEpoch != nil
                && gap.stream_epoch != streamEpoch
            if let epoch = gap.stream_epoch {
                streamEpoch = epoch
            }
            openWaterfall?.mark(
                "stream_replay_gap",
                "requested=\(gap.requested_seq) latest=\(gap.latest_seq) reason=\(gap.reason)"
            )
            if gap.session_id == sessionId {
                // A runtime restart is an unconfirmed cursor boundary. Do not
                // carry its sequence into the next stream; refresh truth.
                lastPubsubSeq = epochChanged || gap.reason == "stream_epoch_changed"
                    ? nil
                    : (gap.latest_seq > 0 ? gap.latest_seq : nil)
                lastWorkspaceRevisionFingerprint = nil
            }
            guard let api = apiFactory(appState.serverURL) else { return }
            requestRealtimeRefresh(api: api, sessionId: sessionId)
        case .heartbeat:
            break
        case .changed(let change):
            let epochChanged = change.stream_epoch != nil
                && streamEpoch != nil
                && change.stream_epoch != streamEpoch
            if let epoch = change.stream_epoch {
                streamEpoch = epoch
            }
            if epochChanged {
                lastPubsubSeq = nil
                lastWorkspaceRevisionFingerprint = nil
                guard let api = apiFactory(appState.serverURL) else { return }
                requestRealtimeRefresh(api: api, sessionId: sessionId)
                return
            }
            // Push wakes refresh compact metadata or the transcript tail,
            // depending on whether the server says rows actually changed.
            let nowMs = Int64(Date().timeIntervalSince1970 * 1000)
            let clockSkewMs = Int(clamping: await stream?.clockSkewMs() ?? 0)
            guard activeSessionId == sessionId,
                  !realtimePaused,
                  !Task.isCancelled
            else { return }
            pendingRealtimeTelemetry = PendingRealtimeTelemetry(
                latestEventId: change.latest_event_id,
                serverFanoutAtMs: change.server_fanout_at_ms,
                clientReceivedAtMs: nowMs,
                clockSkewMs: clockSkewMs,
                catalogCommitSeq: change.catalog_commit_seq,
                pubsubSeq: change.pubsub_seq
            )
            if let seq = change.pubsub_seq {
                lastPubsubSeq = seq
            }
            openWaterfall?.mark(
                "stream_changed",
                "latest=\(change.latest_event_id) seq=\(change.pubsub_seq ?? 0) catalog_commit=\(change.catalog_commit_seq ?? 0) preview=\(change.transcript_preview != nil)"
            )
            if let transcriptPreview = change.transcript_preview?.sessionTranscriptPreview {
                applyRealtimeTranscriptPreview(transcriptPreview, sessionId: sessionId)
                openWaterfall?.mark(
                    "stream_preview_applied",
                    "seq=\(change.pubsub_seq ?? 0) provisional=\(transcriptPreview.isProvisional)"
                )
                if transcriptPreview.isProvisional {
                    return
                }
            }
            guard let api = apiFactory(appState.serverURL) else { return }
            let normalizedServerURL = TranscriptSnapshot.normalizedServerURL(appState.serverURL)
            let authGeneration = SharedAuthStore.authGeneration(for: normalizedServerURL)
            let pending = pendingInputStore.load(
                serverURL: normalizedServerURL,
                sessionId: sessionId,
                authGeneration: authGeneration
            )
            if !pending.isEmpty {
                await reconcilePendingInputs(
                    pending,
                    sessionId: sessionId,
                    appState: appState,
                    authGeneration: authGeneration
                )
            }
            switch change.change_kind {
            case "runtime", "title_update", "read_update":
                // These wakes change native chrome, not transcript rows.
                // Refresh the compact detail lane and leave the expensive
                // mobile-tail/timeline/WebKit path untouched.
                loadPrimaryDetail(api: api, sessionId: sessionId)
            default:
                // The initial snapshot on connect has no pubsub wake behind it.
                let isInitialSnapshot = change.change_kind == nil && (change.pubsub_seq ?? 0) == 0
                requestRealtimeRefresh(
                    api: api,
                    sessionId: sessionId,
                    initialSnapshotEventId: isInitialSnapshot ? change.latest_event_id : nil
                )
            }
        }
    }
    /// The SSE stream got a 401 and stopped its own retry loop. Refresh auth
    /// once, then restart the stream with the rotated cookies. A REST call
    /// drives `LonghouseAPI.data()`, whose built-in 401→/api/auth/refresh→retry
    /// rotates and persists the session cookie as a side effect; the restarted
    /// stream then reads the fresh cookie from `SharedAuthStore`. We attempt
    /// this at most once per stream session to avoid a refresh→401 loop.
    private func handleStreamUnauthorized(sessionId: String, appState: AppState) async {
        // This runs inside the stream's own consuming task. If the scene
        // paused (pauseRealtime cancels that task) bail out — Task.isCancelled
        // is the precise signal that we must not resurrect the stream.
        guard activeSessionId == sessionId, !Task.isCancelled else { return }
        // Second 401 with no successful connect in between: don't refresh-loop.
        // The actor has already stopped its retry loop, so drop our handles to
        // the now-dead stream; a later foreground start() will reattach since
        // it gates on streamTask == nil.
        guard !streamAuthRefreshAttempted else {
            let deadStream = stream
            streamTask = nil
            stream = nil
            await deadStream?.stop()
            return
        }
        streamAuthRefreshAttempted = true
        guard let api = apiFactory(appState.serverURL) else { return }
        // Best-effort: success refreshes cookies; failure leaves content intact
        // and surfaces via refreshErrorMessage on the next reconcile.
        try? await refreshTail(api: api, sessionId: sessionId, allowFailure: true)
        // Re-check after the await: the scene may have paused mid-refresh.
        guard activeSessionId == sessionId, !Task.isCancelled else { return }
        startStream(sessionId: sessionId, appState: appState)
    }

    private func pollTick(sessionId: String, appState: AppState) async {
        guard let api = apiFactory(appState.serverURL) else { return }
        let normalizedServerURL = TranscriptSnapshot.normalizedServerURL(appState.serverURL)
        let authGeneration = SharedAuthStore.authGeneration(for: normalizedServerURL)
        let pending = pendingInputStore.load(
            serverURL: normalizedServerURL,
            sessionId: sessionId,
            authGeneration: authGeneration
        )
        if !pending.isEmpty {
            await reconcilePendingInputs(
                pending,
                sessionId: sessionId,
                appState: appState,
                authGeneration: authGeneration
            )
        }
        try? await refreshTail(api: api, sessionId: sessionId, allowFailure: true)
    }

    /// One durable refresh per burst of wakes. A catalog commit fans out as
    /// several frames within milliseconds; refreshing once per frame turned a
    /// burst of N into N sequential tail reads and N WebKit renders. While a
    /// refresh is in flight the newest wake only marks it dirty, and at most
    /// one follow-up runs when it lands.
    private func requestRealtimeRefresh(
        api: SessionWorkspaceClient,
        sessionId: String,
        initialSnapshotEventId: Int? = nil
    ) {
        if let initialSnapshotEventId {
            realtimeWakeSnapshotEventId = max(realtimeWakeSnapshotEventId ?? initialSnapshotEventId, initialSnapshotEventId)
        } else {
            realtimeWakeNeedsFollowUp = true
        }
        if realtimeRefreshTask != nil {
            realtimeRefreshPending = true
            return
        }
        realtimeRefreshRequestToken &+= 1
        let requestToken = realtimeRefreshRequestToken
        realtimeRefreshTask = Task { [weak self] in
            guard let self else { return }
            // The forced follow-up is a one-shot per joined request: "the
            // request I joined may have captured its snapshot before this
            // wake", which one fetch settles. Re-arming it on every pass
            // instead made the loop depend on a handle it kept re-joining —
            // and a join that resolves without suspending never yields the
            // actor, so nothing could release it and the main thread was
            // pinned for as long as the app lived.
            var forcedFollowUp = false
            repeat {
                self.realtimeRefreshPending = false
                let passSnapshotEventId = self.realtimeWakeSnapshotEventId
                let passNeedsFollowUp = self.realtimeWakeNeedsFollowUp
                self.realtimeWakeSnapshotEventId = nil
                self.realtimeWakeNeedsFollowUp = false
                let joined = await self.refreshTailAfterRealtimeWake(api: api, sessionId: sessionId)
                // Opening a session starts the stream beside the first tail
                // fetch, so the stream's initial snapshot frame joins that
                // fetch. When the fetch already showed the snapshot's latest
                // event, a follow-up re-reads identical content: a second
                // round trip and render on every open.
                let snapshotCovered = !passNeedsFollowUp
                    && passSnapshotEventId.map { $0 <= (self.tailObservedEventId ?? Int.min) } == true
                if joined, !forcedFollowUp, !snapshotCovered {
                    forcedFollowUp = true
                    self.realtimeRefreshPending = true
                } else if !joined {
                    forcedFollowUp = false
                }
            } while self.realtimeRefreshPending
                && self.activeSessionId == sessionId
                && self.realtimeRefreshRequestToken == requestToken
                && !Task.isCancelled
            if self.realtimeRefreshRequestToken == requestToken {
                self.realtimeRefreshTask = nil
            }
        }
    }

    /// Returns whether this pass joined a refresh that then completed
    /// successfully. Only that case earns the caller's follow-up: the request
    /// it joined may have captured its snapshot before this wake. A pass that
    /// fetched for itself needs nothing, and a pass that failed has already
    /// scheduled its own retry — forcing a second attempt alongside that retry
    /// would double-count the failure and defeat the backoff. The loop owns its
    /// own exit condition; this does not re-arm anything by itself.
    @discardableResult
    private func refreshTailAfterRealtimeWake(
        api: SessionWorkspaceClient,
        sessionId: String
    ) async -> Bool {
        guard activeSessionId == sessionId, !realtimePaused else { return false }
        let generation = routeLoadGeneration
        let joinedExistingTail = tailRefreshTask != nil
        do {
            try await refreshTail(api: api, sessionId: sessionId)
            guard isCurrentRoute(sessionId: sessionId, generation: generation) else { return false }
            loadSubagents(api: api, sessionId: sessionId)
            realtimeRefreshFailureCount = 0
            realtimeRefreshRetryTask?.cancel()
            realtimeRefreshRetryTask = nil
            refreshErrorMessage = nil
        } catch is CancellationError {
            return false
        } catch {
            guard isCurrentRoute(sessionId: sessionId, generation: generation), !realtimePaused else {
                return false
            }
            scheduleRealtimeRefreshRetry(api: api, sessionId: sessionId)
            // The failure owns the next attempt. Reporting the join here would
            // force one alongside that retry, which is the double-count this
            // rule exists to avoid.
            return false
        }
        return joinedExistingTail
    }

    private func scheduleRealtimeRefreshRetry(api: SessionWorkspaceClient, sessionId: String) {
        guard activeSessionId == sessionId, !realtimePaused else { return }
        realtimeRefreshFailureCount += 1
        refreshErrorMessage = "Live update delayed. Retrying..."
        let delays = realtimeRefreshRetryDelaysNanoseconds.isEmpty
            ? [1_000_000_000]
            : realtimeRefreshRetryDelaysNanoseconds
        let index = min(
            max(0, realtimeRefreshFailureCount - 1),
            delays.count - 1
        )
        let delay = delays[index]
        realtimeRefreshRetryTask?.cancel()
        realtimeRefreshRetryTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: delay)
            if Task.isCancelled { return }
            guard let self, self.activeSessionId == sessionId, !self.realtimePaused else { return }
            // Route the retry through the coalescing entry point rather than
            // calling the fetch directly. A retry that lands while a refresh is
            // already in flight then gets the same bounded follow-up every
            // other wake gets, instead of joining and dropping it.
            self.requestRealtimeRefresh(api: api, sessionId: sessionId)
        }
    }

    private func applyRealtimeTranscriptPreview(_ preview: SessionTranscriptPreview, sessionId: String) {
        guard activeSessionId == sessionId, !realtimePaused else { return }
        detail = detail?.replacingTranscriptPreview(preview)
        queueCurrentPreviewBuild(sessionId: sessionId)
    }

    private func queueCurrentPreviewBuild(sessionId: String) {
        guard activeSessionId == sessionId, !realtimePaused else { return }
        let currentPreview = detail?.transcriptPreview
        let request = RealtimePreviewBuildRequest(
            buildInput: projectionItemsWithTranscriptPreview(
                lastWorkspaceProjectionItems,
                durableEvents: lastWorkspaceEvents,
                preview: currentPreview
            ),
            preview: currentPreview,
            transcriptReadThrough: pendingTranscriptReadThrough ?? transcriptReadThrough,
            buildRevision: timelineBuildRevision,
            sourceRevision: transcriptInputRevision,
            routeGeneration: routeLoadGeneration,
            sessionId: sessionId
        )
        // Preview frames are frequent while a provider is speaking. Keep one
        // detached build in flight and retain only the newest pending input;
        // cancelling an awaiting wrapper cannot interrupt TimelineBuilder's
        // synchronous work once it has entered the detached task.
        if realtimePreviewBuildTask != nil {
            pendingRealtimePreviewBuild = request
        } else {
            startRealtimePreviewBuild(request)
        }
    }

    private func startRealtimePreviewBuild(_ request: RealtimePreviewBuildRequest) {
        realtimePreviewBuildTask = Task { [weak self] in
            let builtItems = await Task.detached(priority: .userInitiated) {
                TimelineBuilder.build(items: request.buildInput)
            }.value
            guard let self else { return }
            self.finishRealtimePreviewBuild(
                request,
                builtItems: builtItems,
                shouldPublish: !Task.isCancelled
            )
        }
    }

    private func finishRealtimePreviewBuild(
        _ request: RealtimePreviewBuildRequest,
        builtItems: [TimelineItem],
        shouldPublish: Bool
    ) {
        realtimePreviewBuildTask = nil
        let pending = pendingRealtimePreviewBuild
        pendingRealtimePreviewBuild = nil
        var didPublish = false

        let previewIsCurrent = detail?.transcriptPreview == request.preview
        if shouldPublish,
           activeSessionId == request.sessionId,
           routeLoadGeneration == request.routeGeneration,
           !realtimePaused,
           timelineBuildRevision == request.buildRevision,
           transcriptInputRevision == request.sourceRevision,
           (previewIsCurrent || pending != nil) {
            withAnimation(nil) {
                items = builtItems
                if transcriptRowsReconciled {
                    transcriptRowsPublishedPreview = request.preview
                }
                // Publish the watermark only with the rows whose repair build
                // carries it. Retain a newer boundary if another wake arrived
                // while this detached build was running.
                markTranscriptNeedsReadThrough(
                    retainedTranscriptReadThrough(request.transcriptReadThrough)
                )
            }
            didPublish = true
        }

        guard var pending,
              activeSessionId == pending.sessionId,
              !realtimePaused
        else { return }
        // Publishing the completed preview increments timelineBuildRevision
        // through items.didSet. The queued preview is newer than that local
        // publication, so carry the current revision forward instead of
        // discarding the whole burst as stale.
        if didPublish {
            pending.buildRevision = timelineBuildRevision
        }
        startRealtimePreviewBuild(pending)
    }
    /// Bumped on every route change so a body response for the previous
    /// session can never land in this one.
    private var liteBodyGeneration: UInt64 = 0
    /// The server's per-request cap on `/event-bodies` cursors.
    nonisolated static let liteBodyBatchLimit = 20

    /// An expanded row asked for the full bodies of its cut events. Only
    /// cursors of events this screen holds and that a lite page actually cut
    /// are fetched; the rest of the message is ignored.
    func loadToolBodies(cursors requested: [String], sessionId: String, appState: AppState) async {
        guard activeSessionId == sessionId, let api = apiFactory(appState.serverURL) else { return }
        let held = Set(
            lastWorkspaceEvents.compactMap(\.liteBodyCursor)
                + lastWorkspaceProjectionItems.compactMap { $0.event?.liteBodyCursor }
        )
        let wanted = requested.filter {
            held.contains($0) && liteBodies.bodies[$0] == nil
                && !liteBodies.loading.contains($0) && !liteBodies.unavailable.contains($0)
                && liteBodies.mayRetry($0)
        }
        guard !wanted.isEmpty else { return }
        let generation = liteBodyGeneration
        liteBodies.failed.subtract(wanted)
        liteBodies.loading.formUnion(wanted)
        var start = 0
        while start < wanted.count {
            let batch = Array(wanted[start..<min(start + Self.liteBodyBatchLimit, wanted.count)])
            start += batch.count
            let response = try? await api.sessionEventBodies(id: sessionId, cursors: batch)
            guard generation == liteBodyGeneration, activeSessionId == sessionId else { return }
            var next = liteBodies
            next.loading.subtract(batch)
            if let response {
                for body in response.events { next.bodies[body.cursor] = body }
                next.unavailable.formUnion(response.missing)
                // A cursor the server neither answered nor reported missing
                // must not read as loading forever.
                let answered = Set(response.events.map(\.cursor)).union(response.missing)
                let unanswered = batch.filter { !answered.contains($0) }
                next.failed.formUnion(unanswered)
                for cursor in unanswered { next.failedAt[cursor] = Date() }
                openWaterfall?.mark("lite_bodies_loaded", "requested=\(batch.count) missing=\(response.missing.count)")
            } else {
                // The row keeps its preview and says the read failed; opening
                // it again retries.
                next.failed.formUnion(batch)
                for cursor in batch { next.failedAt[cursor] = Date() }
                openWaterfall?.mark("lite_bodies_failed", "requested=\(batch.count)")
            }
            liteBodies = next
        }
    }

    func loadOlder(sessionId: String, appState: AppState) async {
        guard let api = apiFactory(appState.serverURL) else { return }
        await loadOlder(api: api, sessionId: sessionId)
    }


    /// WebKit measured the rendered transcript with too little scroll range for
    /// the near-top callback: a 50-event window may group down to a few rows.
    /// Pull one older page; the next render measures again, so this repeats
    /// until the gesture can reach near-top, history runs out, or a page adds
    /// nothing.
    func fillHistoryForShortViewport(sessionId: String, appState: AppState) async {
        guard let api = apiFactory(appState.serverURL) else { return }
        await fillHistoryForShortViewport(api: api, sessionId: sessionId)
    }

    private func fillHistoryForShortViewport(
        api: SessionWorkspaceClient,
        sessionId: String,
        allowObsoleteRetry: Bool = true
    ) async {
        guard activeSessionId == sessionId else { return }
        guard isTranscriptFrameReady else {
            historyFillPendingFirstFrame = true
            openWaterfall?.mark("history_fill_deferred", "reason=first_frame_pending")
            return
        }
        historyFillPendingFirstFrame = false
        // WebKit reconciles twice per render; the second ask must not read
        // the first page's in-flight state as "added nothing" and stall.
        guard !isLoadingOlder else { return }
        guard historyFillStalledAtLoadedCount != loadedProjectionItemCount else { return }
        let before = loadedProjectionItemCount
        let generation = routeLoadGeneration
        openWaterfall?.mark("history_fill", "loaded=\(before) total=\(totalProjectionItemCount)")
        let added = await loadOlder(api: api, sessionId: sessionId)
        if let added, added == 0, loadedProjectionItemCount == before {
            historyFillStalledAtLoadedCount = before
        } else if added == nil, allowObsoleteRetry {
            // A detached older-page build can become obsolete without
            // changing the document height, so WebKit may never issue a
            // second geometry callback. Give the current route one retry
            // after the invalidating mutation has settled; the retry is
            // deliberately one-shot so a network failure cannot spin.
            Task { [weak self] in
                await Task.yield()
                guard let self,
                      self.activeSessionId == sessionId,
                      self.routeLoadGeneration == generation,
                      !self.realtimePaused
                else { return }
                await self.fillHistoryForShortViewport(
                    api: api,
                    sessionId: sessionId,
                    allowObsoleteRetry: false
                )
            }
        }
    }

    /// Returns how many projection items the older page added. `nil` means
    /// the request or detached build became obsolete; only a real zero means
    /// the server returned no new history and may stall short-viewport fills.
    @discardableResult
    private func loadOlder(api: SessionWorkspaceClient, sessionId: String) async -> Int? {
        guard activeSessionId == sessionId else { return nil }
        guard loadedProjectionItemCount < totalProjectionItemCount else { return 0 }
        guard !isLoadingOlder else {
            openWaterfall?.mark("older_skipped", "reason=in_flight loaded=\(loadedProjectionItemCount)")
            return nil
        }
        let generation = routeLoadGeneration
        let requestOffset = loadedProjectionItemCount
        let requestCursor = tailNextCursor
        let requestSnapshotEventId = tailSnapshotEventId
        // The prefetch join suspends. Claim the lane before awaiting it so two
        // WebKit geometry callbacks cannot both consume the same prefetched
        // page and then issue duplicate older requests.
        isLoadingOlder = true
        defer { isLoadingOlder = false }
        if let prefetchTask,
           prefetchInFlightCursor == requestCursor,
           prefetchInFlightSnapshotEventId == requestSnapshotEventId {
            openWaterfall?.mark("older_joined", "loaded=\(loadedProjectionItemCount)")
            await prefetchTask.value
            guard isCurrentRoute(sessionId: sessionId, generation: generation),
                  tailNextCursor == requestCursor,
                  tailSnapshotEventId == requestSnapshotEventId
            else { return nil }
        }

        guard isCurrentRoute(sessionId: sessionId, generation: generation),
              tailNextCursor == requestCursor,
              tailSnapshotEventId == requestSnapshotEventId
        else { return nil }
        if let prefetchedOlderTail,
           prefetchedOlderCursor == tailNextCursor,
           prefetchedOlderSnapshotEventId == tailSnapshotEventId {
            let before = loadedProjectionItemCount
            let added = await applyOlderTail(prefetchedOlderTail, sessionId: sessionId)
            if added != nil {
                self.prefetchedOlderTail = nil
                self.prefetchedOlderCursor = nil
                self.prefetchedOlderSnapshotEventId = nil
                isLoadingOlder = false
                scheduleOlderPrefetch(api: api, sessionId: sessionId)
            }
            openWaterfall?.mark(
                "older_applied",
                "source=prefetch page_items=\(prefetchedOlderTail.projection.items.count) added=\(added.map { String($0) } ?? "discarded") loaded=\(before)->\(loadedProjectionItemCount) total=\(totalProjectionItemCount)"
            )
            return added
        }

        do {
            let before = requestOffset
            let tail = try await fetchOlderTail(
                api: api,
                sessionId: sessionId,
                offset: requestOffset,
                snapshotEventId: requestSnapshotEventId,
                cursor: requestCursor
            )
            guard isCurrentRoute(sessionId: sessionId, generation: generation),
                  tailNextCursor == requestCursor,
                  tailSnapshotEventId == requestSnapshotEventId
            else { return nil }
            let added = await applyOlderTail(tail, sessionId: sessionId)
            openWaterfall?.mark(
                "older_applied",
                "page_items=\(tail.projection.items.count) added=\(added.map { String($0) } ?? "discarded") loaded=\(before)->\(loadedProjectionItemCount) total=\(totalProjectionItemCount) cursor=\(tail.projection.nextCursor != nil)"
            )
            if added != nil {
                isLoadingOlder = false
                scheduleOlderPrefetch(api: api, sessionId: sessionId)
            }
            return added
        } catch let LonghouseAPIError.structured(_, code, _) where code == "projection_drift" {
            openWaterfall?.mark("older_drift")
            try? await refreshTail(api: api, sessionId: sessionId, allowFailure: true)
        } catch {
            // Older history is opportunistic; keep the visible tail stable.
            openWaterfall?.mark("older_failed", "error=\(error)")
        }
        return nil
    }


    private func refreshTail(api: SessionWorkspaceClient, sessionId: String, allowFailure: Bool = false) async throws {
        guard activeSessionId == sessionId else { return }

        if let tailRefreshTask {
            openWaterfall?.mark("request_joined")
            do {
                try await tailRefreshTask.value
            } catch {
                if !allowFailure { throw error }
            }
            return
        }

        nextTailRefreshToken += 1
        let token = nextTailRefreshToken
        let generation = routeLoadGeneration
        activeTailRefreshToken = token
        let task = Task { [weak self] in
            guard let self else { return }
            // The refresh owns its own handle. Clearing it from this caller's
            // `defer` instead let a *finished* task stay in `tailRefreshTask`
            // whenever a joiner resumed from `await value` first — and a
            // non-nil handle is what tells the realtime wake loop a refresh is
            // still in flight. It then re-armed on every pass, joining a
            // completed task without ever suspending, and spun the main actor.
            defer {
                if self.activeTailRefreshToken == token {
                    self.tailRefreshTask = nil
                    self.activeTailRefreshToken = nil
                }
            }
            try await self.performRefreshTail(
                api: api,
                sessionId: sessionId,
                generation: generation
            )
        }
        tailRefreshTask = task

        do {
            try await task.value
        } catch {
            if !allowFailure { throw error }
        }
    }
    /// The non-destructive form of a blocking load failure. Same cause, and
    /// the same thing for the user to do, without the instruction to reload a
    /// screen that is already showing saved content.
    private static func bannerMessage(for blocking: String) -> String {
        switch blocking {
        case "Couldn't load session. Pull to refresh.":
            return "Live update temporarily unavailable. Showing saved messages."
        case "Session expired.":
            return "Session expired. Pull to refresh."
        default:
            return blocking
        }
    }

    private func shouldAcceptDetail(_ incoming: SessionDetail) -> Bool {
        guard let incomingCommit = incoming.stateFacts.commitSeq else {
            return detail?.stateFacts.commitSeq == nil
        }
        guard let existingCommit = detail?.stateFacts.commitSeq else {
            return true
        }
        return incomingCommit >= existingCommit
    }
    private func shouldAcceptPrimaryDetail(_ incoming: SessionDetail) -> Bool {
        // The tail carries transcript-owned enrichment and wins equal or
        // unknown freshness. A compact detail may replace it only with a
        // provably newer catalog commit.
        guard detailWasLoadedFromTail else {
            return shouldAcceptDetail(incoming)
        }
        guard let incomingCommit = incoming.stateFacts.commitSeq else {
            return false
        }
        guard let existingCommit = detail?.stateFacts.commitSeq else {
            return true
        }
        return incomingCommit > existingCommit
    }

    private func performRefreshTail(
        api: SessionWorkspaceClient,
        sessionId: String,
        generation: Int
    ) async throws {
        guard isCurrentRoute(sessionId: sessionId, generation: generation) else {
            throw CancellationError()
        }

        do {
            let requestStartedAt = Date()
            openWaterfall?.mark("request_start", "limit=\(initialTailLimit)")
            let read = try await readTail(api: api, sessionId: sessionId)
            let tail = read.tail
            let requestMs = Int(Date().timeIntervalSince(requestStartedAt) * 1000)
            guard isCurrentRoute(sessionId: sessionId, generation: generation) else {
                throw CancellationError()
            }
            if !read.isDelta {
                heldTailGenerationId = tail.projection.generationId
                heldTailHeadSessionId = tail.projection.headSessionId
                lastFullTailReadAt = Date()
            }
            openWaterfall?.mark(
                "request_finished",
                "elapsed_ms=\(requestMs) events=\(tail.events.count) total=\(tail.projection.total) delta=\(read.isDelta)"
            )
            if let observed = tail.workspaceRevision?.latestEventId.flatMap(Int.init) {
                tailObservedEventId = max(tailObservedEventId ?? observed, observed)
            }
            // Native metadata is the primary lane. Publish it as soon as the
            // compact tail arrives; TimelineBuilder must never delay the
            // title, runtime dock, or composer.
            if shouldAcceptDetail(tail.session) {
                detailWasLoadedFromTail = true
                if let existing = detail {
                    detail = tail.session.preservingOptionalEnrichment(from: existing)
                } else {
                    detail = tail.session
                }
                openWaterfall?.mark(
                    "tail_detail_loaded",
                    "title_chars=\(tail.session.displayTitle.count)"
                )
            } else {
                openWaterfall?.mark(
                    "detail_discarded",
                    "reason=older_commit incoming=\(tail.session.stateFacts.commitSeq ?? -1) existing=\(detail?.stateFacts.commitSeq ?? -1)"
                )
            }
            let buildStartedAt = Date()
            // Keep the tail result local while its renderable rows are built
            // off-main. A preview or another timeline mutation may win during
            // that suspension; the durable response still gets committed once,
            // and the existing latest-only preview lane repairs native rows.
            let previousTranscriptReadThrough = transcriptReadThrough
            let previousWorkspaceRevisionFingerprint = lastWorkspaceRevisionFingerprint
            let incomingWorkspaceRevisionFingerprint = tail.workspaceRevision?.fingerprint
            let clearingBlockingLoadError = !hasLoadedTranscript
            let acceptedTranscriptPreview = detail?.transcriptPreview
            let acceptedTranscriptReadThrough = self.acceptedTranscriptReadThrough(for: tail.session)
            let mergedEvents = mergeRefreshedTail(read.freshItems.compactMap(\.event))
            let mergedProjectionItems = mergeRefreshedProjectionItems(
                freshTailItems: read.freshItems,
                mergedEvents: mergedEvents
            )
            let refreshedLoadedCount: Int
            if read.isDelta {
                // A delta replaces only rows after its anchor; every older
                // page already loaded stays loaded.
                refreshedLoadedCount = min(
                    tail.projection.total,
                    max(0, loadedProjectionItemCount + mergedProjectionItems.count - lastWorkspaceProjectionItems.count)
                )
            } else {
                refreshedLoadedCount = min(
                    tail.projection.total,
                    max(0, max(tail.projection.total - tail.projection.pageOffset, mergedProjectionItems.count))
                )
            }
            // A delta leaves the older window alone: its cursor, snapshot
            // marker and any prefetched older page all still apply.
            let refreshedNextCursor = read.isDelta ? tailNextCursor : tail.projection.nextCursor
            let refreshedSnapshotEventId = read.isDelta ? tailSnapshotEventId : tail.snapshotEventId
            let keepPrefetchedOlderTail = prefetchedOlderCursor == refreshedNextCursor
                && prefetchedOlderSnapshotEventId == refreshedSnapshotEventId
            let shouldPublishTranscript =
                !transcriptRowsReconciled
                || incomingWorkspaceRevisionFingerprint == nil
                || previousWorkspaceRevisionFingerprint != incomingWorkspaceRevisionFingerprint
                || previousTranscriptReadThrough != acceptedTranscriptReadThrough
                || transcriptRowsPublishedPreview != acceptedTranscriptPreview
            let buildInput: [SessionProjectionItem]?
            if shouldPublishTranscript {
                buildInput = projectionItemsWithTranscriptPreview(
                    mergedProjectionItems,
                    durableEvents: mergedEvents,
                    preview: acceptedTranscriptPreview
                )
            } else {
                buildInput = nil
            }
            let buildRevision = timelineBuildRevision
            let sourceRevision = transcriptInputRevision
            let builtItems: [TimelineItem]?
            if let buildInput {
                builtItems = await Task.detached(priority: .userInitiated) {
                    TimelineBuilder.build(items: buildInput)
                }.value
            } else {
                builtItems = nil
            }
            guard isCurrentRoute(sessionId: sessionId, generation: generation) else {
                throw CancellationError()
            }
            let timelineInputsChanged = builtItems != nil && timelineBuildRevision != buildRevision
            let durableInputsChanged = builtItems != nil && transcriptInputRevision != sourceRevision
            let previewChanged = detail?.transcriptPreview != acceptedTranscriptPreview
            let shouldApplyBuiltItems =
                builtItems != nil
                && !timelineInputsChanged
                && !durableInputsChanged
                && !previewChanged

            // The primary detail request may have published a newer commit
            // while the detached build was running. Never replace it with this
            // older tail; repair the native preview through the bounded queue.
            let buildMs = Int(Date().timeIntervalSince(buildStartedAt) * 1000)
            let needsPreviewRepair = shouldPublishTranscript && !shouldApplyBuiltItems

            // Batch native/transcript state so SwiftUI sees one settled
            // snapshot, while metadata already remains useful during the
            // detached build above.
            withAnimation(nil) {
                lastWorkspaceEvents = mergedEvents
                lastWorkspaceProjectionItems = mergedProjectionItems
                if shouldPublishTranscript {
                    transcriptInputRevision &+= 1
                }
                loadedProjectionItemCount = refreshedLoadedCount
                totalProjectionItemCount = tail.projection.total
                tailSnapshotEventId = refreshedSnapshotEventId
                tailNextCursor = refreshedNextCursor
                if let incomingWorkspaceRevisionFingerprint {
                    lastWorkspaceRevisionFingerprint = incomingWorkspaceRevisionFingerprint
                }
                if !keepPrefetchedOlderTail {
                    prefetchedOlderTail = nil
                    prefetchedOlderCursor = nil
                    prefetchedOlderSnapshotEventId = nil
                }
                reconcileSubmittedInputs(with: mergedEvents)
                if shouldApplyBuiltItems, let builtItems {
                    self.items = builtItems
                    transcriptRowsPublishedPreview = acceptedTranscriptPreview
                    // Rows and their unread boundary become visible as one
                    // publish. A stale detached build must not expose its
                    // newer watermark against the previous rows.
                    markTranscriptNeedsReadThrough(acceptedTranscriptReadThrough)
                } else if !shouldPublishTranscript {
                    markTranscriptNeedsReadThrough(acceptedTranscriptReadThrough)
                } else {
                    pendingTranscriptReadThrough = retainedTranscriptReadThrough(
                        acceptedTranscriptReadThrough
                    )
                }
                transcriptRowsReconciled = true
                self.hasLoadedTranscript = true
                if clearingBlockingLoadError, errorMessage != nil {
                    // A successful tail replaces a blocking cold-load error.
                    // Do not clear send errors from populated data.
                    self.errorMessage = nil
                }
            }
            openWaterfall?.mark(
                shouldApplyBuiltItems
                    ? (shouldPublishTranscript ? "timeline_built" : "timeline_unchanged")
                    : "timeline_waiting_for_preview",
                "events=\(mergedEvents.count) items=\(items.count) elapsed_ms=\(shouldPublishTranscript ? buildMs : 0)"
            )
            // A same-fingerprint wake has no new durable rows to persist.
            // Avoid re-encoding and rewriting the full snapshot on every
            // stream/poll heartbeat; watermark changes still publish and
            // take this path because they make the payload new.
            if shouldPublishTranscript {
                saveCurrentCache()
            }
            if needsPreviewRepair {
                queueCurrentPreviewBuild(sessionId: sessionId)
            }
            scheduleOlderPrefetch(api: api, sessionId: sessionId)
        } catch {
            if isCurrentRoute(sessionId: sessionId, generation: generation) {
                openWaterfall?.mark("request_failed", "error=\(error)")
            }
            throw error
        }
    }

    private struct TailRead {
        let tail: SessionMobileTailResponse
        /// The latest window, or for a delta the held rows from its anchor on
        /// with the new rows spliced in, shaped for the tail merge below.
        let freshItems: [SessionProjectionItem]
        let isDelta: Bool
    }

    /// Rows a delta re-reads behind the newest one: tool calls near the tail
    /// still change state (running -> completed) after newer rows land.
    nonisolated static let deltaAnchorDepth = 20
    /// A full tail read at least this often, as a correction for anything a
    /// run of deltas could miss.
    nonisolated static let fullTailReadInterval: TimeInterval = 60

    private func readTail(api: SessionWorkspaceClient, sessionId: String) async throws -> TailRead {
        if let delta = await readTailDelta(api: api, sessionId: sessionId) {
            return delta
        }
        let tail = try await api.sessionMobileTail(
            id: sessionId,
            limit: initialTailLimit,
            offset: 0,
            branchMode: "head",
            snapshotEventId: nil,
            cursor: nil
        )
        return TailRead(tail: tail, freshItems: tail.projection.items, isDelta: false)
    }

    /// Events after a settled row this screen holds, with the header, in one
    /// request. Nil means read the latest window instead: nothing held yet, a
    /// correction is due, the server answered with something other than a
    /// delta (an older server, 409 for a re-rendered generation, more than a
    /// page of new rows), or the held rows moved while the request was out.
    private func readTailDelta(api: SessionWorkspaceClient, sessionId: String) async -> TailRead? {
        guard hasLoadedTranscript, transcriptRowsReconciled,
              let generationId = heldTailGenerationId,
              let headSessionId = heldTailHeadSessionId,
              let lastFull = lastFullTailReadAt,
              Date().timeIntervalSince(lastFull) < Self.fullTailReadInterval,
              let anchorIndex = Self.deltaAnchorIndex(in: lastWorkspaceProjectionItems),
              let anchorCursor = lastWorkspaceProjectionItems[anchorIndex].event?.cursor
        else { return nil }
        let delta: SessionMobileTailResponse
        do {
            guard let response = try await api.sessionMobileTailDelta(
                id: sessionId,
                afterCursor: anchorCursor,
                limit: initialTailLimit
            ) else { return nil }
            delta = response
        } catch {
            openWaterfall?.mark("delta_fallback", "error=\(error)")
            return nil
        }
        guard delta.projection.generationId == generationId,
              delta.projection.headSessionId == headSessionId,
              delta.projection.hasMore != true,
              // Splice against the rows as they are now; an older page or
              // another refresh may have landed while this was in flight.
              let liveAnchor = lastWorkspaceProjectionItems.firstIndex(where: { $0.event?.cursor == anchorCursor })
        else {
            openWaterfall?.mark("delta_fallback", "reason=does_not_continue")
            return nil
        }
        let combined = Array(lastWorkspaceProjectionItems[...liveAnchor]) + delta.projection.items
        let tailWindow = min(initialTailLimit, totalProjectionItemCount)
        let fresh = loadedProjectionItemCount > tailWindow ? Array(combined[liveAnchor...]) : combined
        return TailRead(tail: delta, freshItems: fresh, isDelta: true)
    }

    /// The newest held row a delta can safely read after: `deltaAnchorDepth`
    /// rows back, or before the oldest tool call still running, and always a
    /// durable event with a cursor.
    nonisolated static func deltaAnchorIndex(in items: [SessionProjectionItem]) -> Int? {
        guard !items.isEmpty else { return nil }
        var index = max(0, items.count - 1 - deltaAnchorDepth)
        if let running = items.firstIndex(where: { $0.event?.toolCallState == .running }) {
            index = min(index, running - 1)
        }
        while index >= 0 {
            if let event = items[index].event,
               event.eventOrigin != "live_provisional",
               !event.isSynthetic,
               event.cursor?.isEmpty == false {
                return index
            }
            index -= 1
        }
        return nil
    }

    private func mergeRefreshedTail(_ freshTailEvents: [SessionEvent]) -> [SessionEvent] {
        let currentTailWindowCount = min(initialTailLimit, totalProjectionItemCount)
        guard loadedProjectionItemCount > currentTailWindowCount else {
            return freshTailEvents
        }
        guard let firstFreshTailEvent = freshTailEvents.first else {
            return lastWorkspaceEvents
        }

        let freshTailEventIds = Set(freshTailEvents.map(\.id))
        let olderEvents: [SessionEvent]
        if let firstFreshIndex = lastWorkspaceEvents.firstIndex(where: { $0.id == firstFreshTailEvent.id }) {
            olderEvents = lastWorkspaceEvents[..<firstFreshIndex].filter { !freshTailEventIds.contains($0.id) }
        } else {
            olderEvents = lastWorkspaceEvents.filter { event in
                event.isOrdered(before: firstFreshTailEvent) != false && !freshTailEventIds.contains(event.id)
            }
        }
        return olderEvents + freshTailEvents
    }

    private func mergeRefreshedProjectionItems(
        freshTailItems: [SessionProjectionItem],
        mergedEvents: [SessionEvent]
    ) -> [SessionProjectionItem] {
        let currentTailWindowCount = min(initialTailLimit, totalProjectionItemCount)
        guard loadedProjectionItemCount > currentTailWindowCount else {
            return freshTailItems
        }
        guard let firstFreshEvent = freshTailItems.compactMap(\.event).first else {
            return freshTailItems
        }

        let freshItemIds = Set(freshTailItems.map(\.id))
        if let firstFreshItemId = freshTailItems.first?.id,
           let firstFreshIndex = lastWorkspaceProjectionItems.firstIndex(where: { $0.id == firstFreshItemId }) {
            let olderItems = lastWorkspaceProjectionItems[..<firstFreshIndex].filter {
                !freshItemIds.contains($0.id)
            }
            return olderItems + freshTailItems
        }

        let freshEventIds = Set(freshTailItems.compactMap(\.event?.id))
        let freshActionIds = Set(freshTailItems.compactMap(\.action?.id))
        let olderMergedEventIds = Set(mergedEvents.filter {
            $0.isOrdered(before: firstFreshEvent) != false
        }.map(\.id))
        let olderItems = lastWorkspaceProjectionItems.filter { item in
            if let event = item.event {
                return olderMergedEventIds.contains(event.id) && !freshEventIds.contains(event.id)
            }
            if let action = item.action {
                return item.timestamp < firstFreshEvent.timestamp && !freshActionIds.contains(action.id)
            }
            return false
        }
        return olderItems + freshTailItems
    }

    private func scheduleOlderPrefetch(api: SessionWorkspaceClient, sessionId: String) {
        guard enableRealtime else { return }
        guard activeSessionId == sessionId else { return }
        guard isTranscriptFrameReady else { return }
        guard loadedProjectionItemCount < totalProjectionItemCount else { return }
        guard !isLoadingOlder else { return }
        let cursor = tailNextCursor
        let offset = loadedProjectionItemCount
        let snapshotEventId = tailSnapshotEventId
        let generation = routeLoadGeneration
        let hasStoredPrefetch = prefetchedOlderCursor == cursor
            && prefetchedOlderSnapshotEventId == snapshotEventId
        let hasInFlightPrefetch = prefetchInFlightCursor == cursor
            && prefetchInFlightSnapshotEventId == snapshotEventId
        guard !hasStoredPrefetch && !hasInFlightPrefetch else { return }

        prefetchTask?.cancel()
        prefetchTask = nil
        nextPrefetchToken += 1
        let prefetchToken = nextPrefetchToken
        prefetchInFlightCursor = cursor
        prefetchInFlightSnapshotEventId = snapshotEventId
        prefetchInFlightToken = prefetchToken
        prefetchTask = Task { [weak self] in
            guard let self else { return }
            defer {
                if self.prefetchInFlightToken == prefetchToken {
                    self.prefetchInFlightCursor = nil
                    self.prefetchInFlightSnapshotEventId = nil
                    self.prefetchInFlightToken = nil
                    self.prefetchTask = nil
                }
            }
            do {
                let tail = try await self.fetchOlderTail(
                    api: api,
                    sessionId: sessionId,
                    offset: offset,
                    snapshotEventId: snapshotEventId,
                    cursor: cursor
                )
                guard !Task.isCancelled,
                      self.activeSessionId == sessionId,
                      self.routeLoadGeneration == generation,
                      self.tailNextCursor == cursor,
                      self.tailSnapshotEventId == snapshotEventId
                else { return }
                self.prefetchedOlderTail = tail
                self.prefetchedOlderCursor = cursor
                self.prefetchedOlderSnapshotEventId = snapshotEventId
            } catch {
                guard !Task.isCancelled,
                      self.activeSessionId == sessionId,
                      self.routeLoadGeneration == generation,
                      self.tailNextCursor == cursor,
                      self.tailSnapshotEventId == snapshotEventId
                else { return }
                self.prefetchedOlderTail = nil
                self.prefetchedOlderCursor = nil
                self.prefetchedOlderSnapshotEventId = nil
            }
        }
    }

    private func fetchOlderTail(
        api: SessionWorkspaceClient,
        sessionId: String,
        offset: Int,
        snapshotEventId: String?,
        cursor: String?
    ) async throws -> SessionMobileTailResponse {
        try await api.sessionMobileTail(
            id: sessionId,
            limit: olderPageLimit,
            offset: offset,
            branchMode: "head",
            snapshotEventId: snapshotEventId,
            cursor: cursor
        )
    }
    /// Returns how many projection items the page actually added. `nil` means
    /// that the page became obsolete while its rows were being built.
    @discardableResult
    private func applyOlderTail(
        _ tail: SessionMobileTailResponse,
        sessionId: String
    ) async -> Int? {
        guard activeSessionId == sessionId, !realtimePaused else { return nil }
        let generation = routeLoadGeneration
        let expectedCursor = tailNextCursor
        let expectedSnapshotEventId = tailSnapshotEventId
        let existingItemIds = Set(lastWorkspaceProjectionItems.map(\.id))
        let olderProjectionItems = tail.projection.items.filter { !existingItemIds.contains($0.id) }
        guard !olderProjectionItems.isEmpty else {
            // An empty page is a stall signal, not permission for an older
            // response to replace the cursor a newer tail already owns.
            totalProjectionItemCount = max(totalProjectionItemCount, tail.projection.total)
            lastWorkspaceRevisionFingerprint = tail.workspaceRevision?.fingerprint ?? lastWorkspaceRevisionFingerprint
            return 0
        }

        let existingEventIds = Set(lastWorkspaceEvents.map(\.id))
        let olderEvents = olderProjectionItems.compactMap(\.event).filter { !existingEventIds.contains($0.id) }
        let combinedEvents = olderEvents + lastWorkspaceEvents
        let combinedProjectionItems = olderProjectionItems + lastWorkspaceProjectionItems
        let preview = detail?.transcriptPreview
        let buildInput = projectionItemsWithTranscriptPreview(
            combinedProjectionItems,
            durableEvents: combinedEvents,
            preview: preview
        )
        let buildRevision = timelineBuildRevision
        let sourceRevision = transcriptInputRevision
        let builtItems = await Task.detached(priority: .userInitiated) {
            TimelineBuilder.build(items: buildInput)
        }.value
        guard isCurrentRoute(sessionId: sessionId, generation: generation),
              tailNextCursor == expectedCursor,
              tailSnapshotEventId == expectedSnapshotEventId,
              timelineBuildRevision == buildRevision,
              transcriptInputRevision == sourceRevision,
              detail?.transcriptPreview == preview
        else {
            openWaterfall?.mark("older_build_discarded", "reason=newer_timeline_input")
            return nil
        }

        totalProjectionItemCount = tail.projection.total
        tailNextCursor = tail.projection.nextCursor
        lastWorkspaceRevisionFingerprint = tail.workspaceRevision?.fingerprint ?? lastWorkspaceRevisionFingerprint
        loadedProjectionItemCount = min(
            totalProjectionItemCount,
            loadedProjectionItemCount + olderProjectionItems.count
        )
        transcriptInputRevision &+= 1
        lastWorkspaceEvents = combinedEvents
        lastWorkspaceProjectionItems = combinedProjectionItems
        items = builtItems
        if transcriptRowsReconciled {
            transcriptRowsPublishedPreview = preview
        }
        reconcileSubmittedInputs(with: lastWorkspaceEvents)
        saveCurrentCache()
        return olderProjectionItems.count
    }

    private func projectionItemsWithTranscriptPreview(
        _ projectionItems: [SessionProjectionItem],
        durableEvents: [SessionEvent],
        preview: SessionTranscriptPreview?
    ) -> [SessionProjectionItem] {
        let baseItems = projectionItems.isEmpty && !durableEvents.isEmpty
            ? projectionItemsFromEvents(durableEvents)
            : projectionItems
        let visibleEvents = TranscriptPreviewProjection.visibleEvents(
            durableEvents: durableEvents,
            preview: preview
        )
        guard visibleEvents.count != durableEvents.count,
              let synthetic = visibleEvents.last,
              synthetic.isSynthetic
        else {
            return baseItems
        }
        return baseItems + projectionItemsFromEvents([synthetic])
    }

    private func projectionItemsFromEvents(_ events: [SessionEvent]) -> [SessionProjectionItem] {
        events.map { event in
            SessionProjectionItem(
                kind: "event",
                sessionId: activeSessionId ?? detail?.id ?? "",
                timestamp: event.timestamp,
                event: event,
                continuedFromSessionId: nil,
                continuationKind: nil,
                originLabel: nil,
                parentOriginLabel: nil,
                parentContinuationKind: nil,
                branchedFromEventId: nil
            )
        }
    }

    private func adoptResumeMetadata(from snapshot: TranscriptSnapshot) {
        if lastPubsubSeq == nil {
            lastPubsubSeq = snapshot.lastPubsubSeq
        }
        if streamEpoch == nil {
            streamEpoch = snapshot.streamEpoch
        }
        if lastWorkspaceRevisionFingerprint == nil {
            lastWorkspaceRevisionFingerprint = snapshot.workspaceRevisionFingerprint
        }
    }

    private func applySnapshot(
        _ restored: TranscriptSnapshotStore.Restored,
        sessionId: String,
        generation: Int
    ) async -> Bool {
        guard isCurrentRoute(sessionId: sessionId, generation: generation) else {
            return false
        }
        guard !hasLoadedTranscript else {
            adoptResumeMetadata(from: restored.snapshot)
            openWaterfall?.mark("cache_discarded", "reason=tail_won")
            return false
        }

        let snapshot = restored.snapshot
        let projectionItems = snapshot.projectionItems ?? projectionItemsFromEvents(snapshot.events)
        let buildRevision = timelineBuildRevision
        let builtItems = await Task.detached(priority: .userInitiated) {
            TimelineBuilder.build(items: projectionItems)
        }.value

        guard isCurrentRoute(sessionId: sessionId, generation: generation) else {
            return false
        }
        guard !hasLoadedTranscript else {
            // The tail can win while the cache rows are being built. Keep its
            // state and only borrow resume metadata that it did not provide.
            adoptResumeMetadata(from: snapshot)
            openWaterfall?.mark("cache_discarded", "reason=tail_won")
            return false
        }
        guard timelineBuildRevision == buildRevision else {
            openWaterfall?.mark("cache_discarded", "reason=newer_timeline_input")
            return false
        }

        // A failure that landed before the cache painted was reported as a
        // blocking error. Once saved rows are on screen the same failure has
        // to read as the banner — the blocking copy tells the user to reload a
        // screen that already has content — and which of the two they saw
        // otherwise depended on whether the disk read beat the network.
        let blockingLoadError = errorMessage.map(Self.bannerMessage(for:))
        let shouldApplySnapshotDetail = !detailWasLoadedFromPrimary
            && !detailWasLoadedFromTail
        // The cache becomes visible in one commit. Nothing mutates the
        // workspace/tail state before the detached build has passed the
        // route, transcript, and revision fences above.
        withAnimation(nil) {
            if shouldApplySnapshotDetail {
                detail = snapshot.detail
                // A restored snapshot provides immediate chrome, but it does
                // not outrank a fresh compact-detail response.
            }
            // The watermark is the exact transcript boundary represented by
            // the cached rows. Older cache files have no safe boundary; keep
            // that absence instead of deriving acknowledgment from metadata.
            markTranscriptNeedsReadThrough(snapshot.transcriptReadThrough)
            lastWorkspaceEvents = snapshot.events
            lastWorkspaceProjectionItems = projectionItems
            transcriptInputRevision &+= 1
            loadedProjectionItemCount = snapshot.loadedProjectionItemCount
            totalProjectionItemCount = snapshot.totalProjectionItemCount
            tailSnapshotEventId = snapshot.tailSnapshotEventId
            tailNextCursor = snapshot.tailNextCursor
            adoptResumeMetadata(from: snapshot)
            prefetchedOlderTail = nil
            prefetchedOlderCursor = nil
            prefetchedOlderSnapshotEventId = nil
            prefetchInFlightCursor = nil
            prefetchInFlightSnapshotEventId = nil
            prefetchInFlightToken = nil
            transcriptRowsReconciled = false
            transcriptRowsPublishedPreview = nil
            items = builtItems
            hasLoadedTranscript = true
            isInitialLoading = false
            errorMessage = nil
            if refreshErrorMessage == nil {
                // The tail can fail before disk hydration finishes. Keep that
                // failure visible as a non-blocking warning once saved
                // content is on screen instead of silently presenting stale
                // data as current.
                refreshErrorMessage = blockingLoadError
            }
        }
        openWaterfall?.mark(
            "cache_applied",
            "tier=\(restored.tier.rawValue) events=\(snapshot.events.count) items=\(items.count)"
        )
        return true
    }

    /// Background reconcile that never erases on-screen content. A failure
    /// surfaces as a thin banner (`refreshErrorMessage`); success clears it.
    private func refreshInBackground(
        api: SessionWorkspaceClient,
        sessionId: String,
        generation: Int
    ) async {
        do {
            try await refreshTail(api: api, sessionId: sessionId)
            guard isCurrentRoute(sessionId: sessionId, generation: generation) else { return }
            refreshErrorMessage = nil
        } catch is CancellationError {
            return
        } catch LonghouseAPIError.notAuthenticated {
            guard isCurrentRoute(sessionId: sessionId, generation: generation) else { return }
            refreshErrorMessage = "Session expired. Pull to refresh."
        } catch {
            guard isCurrentRoute(sessionId: sessionId, generation: generation) else { return }
            refreshErrorMessage = "Live update temporarily unavailable. Showing saved messages."
        }
        guard isCurrentRoute(sessionId: sessionId, generation: generation) else { return }
        if let api = apiFactory(activeServerURL ?? "") {
            scheduleOlderPrefetch(api: api, sessionId: sessionId)
        }
    }
    private func saveCurrentCache() {
        guard let activeServerURL, let activeSessionId, let detail else { return }
        snapshotStore?.save(
            serverURL: activeServerURL,
            sessionId: activeSessionId,
            snapshot: TranscriptSnapshot(
                detail: detail.withoutTranscriptPreview,
                events: lastWorkspaceEvents,
                projectionItems: lastWorkspaceProjectionItems,
                loadedProjectionItemCount: loadedProjectionItemCount,
                totalProjectionItemCount: totalProjectionItemCount,
                tailSnapshotEventId: tailSnapshotEventId,
                transcriptReadThrough: transcriptReadThrough,
                tailNextCursor: tailNextCursor,
                lastPubsubSeq: lastPubsubSeq,
                streamEpoch: streamEpoch,
                workspaceRevisionFingerprint: lastWorkspaceRevisionFingerprint
            )
        )
    }

    /// Re-read the queue line from the served receipts, so it follows the queue
    /// draining or expiring instead of keeping the count from the last send.
    private func refreshQueuedIndicator() {
        guard let detail, let receipts = detail.inputReceipts else { return }
        let counts = QueuedInputIndicator.counts(
            receipts: receipts,
            ownClientRequestIds: Set(submittedInputs.map(\.clientRequestId)),
            ownQueuedClientRequestIds: Set(
                submittedInputs.filter { $0.phase == .queued && $0.turnId == nil }.map(\.clientRequestId)
            )
        )
        let total = Self.queuedLineTotal(counts, isConsole: detail.stateFacts.mode == "console")
        if queuedInputCount != total { queuedInputCount = total }
        if queuedElsewhereCount != counts.elsewhere { queuedElsewhereCount = counts.elsewhere }
    }

    /// Console shows each of this phone's queued turns as its own bubble, so its
    /// line counts only what another sender parked: that turn has no bubble here
    /// and was otherwise invisible until it started.
    static func queuedLineTotal(_ counts: (total: Int, elsewhere: Int), isConsole: Bool) -> Int {
        isConsole ? counts.elsewhere : counts.total
    }

    private func updateSubmittedInput(
        _ id: String,
        phase: SubmittedInputPhase,
        serverInputId: Int?,
        liveInputId: String? = nil,
        turnId: String? = nil,
        runId: String? = nil,
        deliveryStatus: String? = nil,
        lastError: String?
    ) {
        guard let index = submittedInputs.firstIndex(where: { $0.id == id }) else { return }
        submittedInputs[index].phase = phase
        submittedInputs[index].serverInputId = serverInputId
        if let liveInputId { submittedInputs[index].liveInputId = liveInputId }
        if let turnId { submittedInputs[index].turnId = turnId }
        if let runId { submittedInputs[index].runId = runId }
        if let deliveryStatus { submittedInputs[index].deliveryStatus = deliveryStatus }
        submittedInputs[index].lastError = lastError
    }



    private func reconcileSubmittedInputs(with events: [SessionEvent]) {
        guard !submittedInputs.isEmpty else { return }
        let pendingBefore = submittedInputs.count
        let resolved = Self.resolvedSubmittedInputIds(
            submittedInputs: submittedInputs,
            events: events,
            receipts: detail?.inputReceipts ?? []
        )
        if !resolved.isEmpty {
            if let activeServerURL, let activeSessionId {
                let authGeneration = SharedAuthStore.authGeneration(for: activeServerURL)
                for id in resolved {
                    pendingInputStore.remove(
                        serverURL: activeServerURL,
                        sessionId: activeSessionId,
                        authGeneration: authGeneration,
                        clientRequestId: id
                    )
                }
            }
            submittedInputs.removeAll { resolved.contains($0.id) }
        }
        let userEvents = events.filter { $0.role == "user" && $0.isHeadBranch }
        let newestUserEvent = userEvents.last.map { "\($0.timestamp) origin=\($0.inputOrigin != nil) text_chars=\($0.contentText?.count ?? -1)" } ?? "none"
        let pendingSummary = submittedInputs
            .map { "\($0.phase):\(Int(Date().timeIntervalSince($0.createdAt)))s:\($0.text.count)ch" }
            .joined(separator: ",")
        let linkedReceipts = (detail?.inputReceipts ?? []).filter { $0.eventId != nil }.count
        openWaterfall?.mark(
            "reconcile_inputs",
            "before=\(pendingBefore) after=\(submittedInputs.count) events=\(events.count) user_events=\(userEvents.count) linked_receipts=\(linkedReceipts) newest_user=\(newestUserEvent) pending=[\(pendingSummary)]"
        )
    }

    /// An optimistic send row is resolved by its served receipt. The server
    /// links each delivered receipt to the durable user event it became at
    /// ingest, so the receipt says the echo exists even when a long turn has
    /// already pushed that event out of the loaded tail. A loaded event stamped
    /// with the same origin is the same fact seen from the page. Where the
    /// server's linker refused an ambiguous resend, a loaded row with the same
    /// text written after that receipt was accepted stands for it, one row per
    /// receipt (`UnrecordedInputs.shownByTranscript`); the local draft's own
    /// text never decides. A settled receipt with no row hands the row over to
    /// the served receipt, which every client places at its send time.
    static func resolvedSubmittedInputIds(
        submittedInputs: [SubmittedInput],
        events: [SessionEvent],
        receipts: [SessionInputReceipt]
    ) -> Set<String> {
        let linkedRequestIds = UnrecordedInputs.shownByTranscript(
            receipts: receipts,
            userEvents: events.filter { $0.role == "user" },
            windowStart: events
                .filter(\.isHeadBranch)
                .compactMap { LonghouseDateParser.parse($0.timestamp) }
                .min()
        )
        // Delivered and settled, with no transcript row: the served receipt now
        // stands at its send time for every client, so this phone's own row
        // hands over to it instead of sitting at the tail as "Sent" forever.
        let settledRequestIds = Set(receipts.compactMap { receipt -> String? in
            guard receipt.eventId == nil,
                  receipt.createdAt.flatMap(LonghouseDateParser.parse) != nil,
                  UnrecordedInputs.isSettledDelivery(receipt),
                  !UnrecordedInputs.failedBeforeRecorded(receipt)
            else { return nil }
            return receipt.clientRequestId
        })
        var resolved = Set<String>()
        for input in submittedInputs {
            guard input.phase == .sent
                || input.phase == .queued
                || input.phase == .submitting
                || input.phase == .working
                || input.phase == .couldNotConfirm
            else { continue }
            // A Console turn's echo can land while the turn still runs; once
            // the server accepted it (.working or .sent) the echo replaces the
            // row. A queued turn has not started, so it keeps its row.
            if input.turnId != nil && input.phase != .sent && input.phase != .working { continue }
            if linkedRequestIds.contains(input.clientRequestId) {
                resolved.insert(input.id)
                continue
            }
            if input.phase == .sent, settledRequestIds.contains(input.clientRequestId) {
                resolved.insert(input.id)
                continue
            }
            let echoed = events.contains { event in
                guard event.role == "user", event.isHeadBranch, let origin = event.inputOrigin else { return false }
                if let serverInputId = input.serverInputId, origin.sessionInputId == serverInputId { return true }
                return origin.clientRequestId == input.clientRequestId
            }
            if echoed { resolved.insert(input.id) }
        }
        return resolved
    }

    private func sendFailureMessage(for error: Error) -> String {
        switch error {
        case LonghouseAPIError.upstreamFailed:
            return "Longhouse couldn't confirm delivery. Refreshing to check whether it landed."
        case LonghouseAPIError.requestFailed:
            return "Longhouse couldn't confirm delivery. Refreshing to check whether it landed."
        case LonghouseAPIError.httpRejected(_, let message):
            return message
        case LonghouseAPIError.unexpectedResponse(let message):
            return message
        case LonghouseAPIError.serviceUnavailable:
            return "Longhouse is temporarily unavailable. Refreshing to check whether it landed."
        case LonghouseAPIError.structured(_, _, let message):
            return message.isEmpty ? "Longhouse couldn't send this message." : message
        case is DecodingError:
            return "Longhouse returned an unexpected send response. Refreshing to check whether it landed."
        case let urlError as URLError:
            if urlError.code == .notConnectedToInternet || urlError.code == .networkConnectionLost {
                return "The network dropped before Longhouse could confirm delivery. Refreshing to check whether it landed."
            }
            return "Longhouse couldn't confirm delivery. Refreshing to check whether it landed."
        default:
            return error.localizedDescription
        }
    }

    private func isUncertainDeliveryError(_ value: String?) -> Bool {
        guard let value else { return false }
        let code = value
            .split(separator: ":", maxSplits: 1)
            .first
            .map(String.init)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased() ?? ""
        switch code {
        case "delivery_unknown",
             "provider_unknown",
             "provider_delivery_unknown",
             "input_receipt_unknown",
             "input_dispatch_in_flight",
             "runtime_draining":
            return true
        default:
            return false
        }
    }

    private func sendConfirmationMayHaveLanded(_ error: Error) -> Bool {
        switch error {
        case let apiError as LonghouseAPIError:
            switch apiError {
            case .structured(_, _, _):
                return apiError.isRuntimeDraining || apiError.isProviderDeliveryUnknown
            case .upstreamFailed,
                 .requestFailed,
                 .unexpectedResponse,
                 .serviceUnavailable:
                return true
            case .notAuthenticated, .conflict, .httpRejected(_, _), .runtimeRestarting:
                return false
            }
        case is DecodingError:
            return true
        case let urlError as URLError:
            switch urlError.code {
            case .notConnectedToInternet,
                 .networkConnectionLost,
                 .timedOut,
                 .cannotConnectToHost,
                 .cannotFindHost,
                 .dnsLookupFailed:
                return true
            default:
                return false
            }
        default:
            return false
        }
    }

    private func reportRenderBeacon(
        api: SessionWorkspaceClient,
        sessionId: String,
        events: [SessionEvent],
        webkitDiagnostics: RenderBeaconReporter.WebKitDiagnostics?
    ) async {
        guard let latest = events.last else { return }
        let pendingTelemetry = pendingRealtimeTelemetry
        let eventForBeacon = pendingTelemetry.flatMap { pending in
            events.last(where: { $0.legacyNumericId == pending.latestEventId })
        } ?? latest
        guard let emittedAt = LonghouseDateParser.parse(eventForBeacon.timestamp) else { return }
        let managed = detail?.stateFacts.controlOwnership == "owned"
        let realtimeTelemetry = pendingTelemetry?.latestEventId == eventForBeacon.legacyNumericId
            ? pendingTelemetry
            : nil
        if let payload = await RenderBeaconReporter.shared.payload(
            sessionId: sessionId,
            latestEventId: eventForBeacon.id,
            emittedAt: emittedAt,
            managed: managed,
            clockSkewMs: realtimeTelemetry?.clockSkewMs ?? 0,
            serverFanoutAtMs: realtimeTelemetry?.serverFanoutAtMs,
            clientReceivedAtMs: realtimeTelemetry?.clientReceivedAtMs,
            pubsubSeq: realtimeTelemetry?.pubsubSeq,
            stateCommitSeq: realtimeTelemetry?.catalogCommitSeq,
            statePhase: detail?.stateFacts.activityState,
            stateObservedAtMs: detail?.stateFacts.activityObservedAt.flatMap { LonghouseDateParser.parse($0) }
                .map { Int64($0.timeIntervalSince1970 * 1000) },
            webkit: webkitDiagnostics
        ) {
            await api.postRenderBeacon(payload)
        }
        if pendingTelemetry != nil {
            pendingRealtimeTelemetry = nil
        }
    }

    private func reportStateRenderBeacon(
        api: SessionWorkspaceClient,
        sessionId: String,
        webkitDiagnostics: RenderBeaconReporter.WebKitDiagnostics?
    ) async {
        if let stage = webkitDiagnostics?.stage, stage != "rendered" { return }
        guard let pendingTelemetry = pendingRealtimeTelemetry,
              let catalogCommitSeq = pendingTelemetry.catalogCommitSeq,
              catalogCommitSeq > 0,
              let serverFanoutAtMs = pendingTelemetry.serverFanoutAtMs else {
            return
        }
        let managed = detail?.stateFacts.controlOwnership == "owned"
        guard let payload = await RenderBeaconReporter.shared.payload(
            sessionId: sessionId,
            latestEventId: "state:\(catalogCommitSeq)",
            emittedAt: Date(timeIntervalSince1970: TimeInterval(serverFanoutAtMs) / 1000),
            managed: managed,
            clockSkewMs: pendingTelemetry.clockSkewMs,
            serverFanoutAtMs: serverFanoutAtMs,
            clientReceivedAtMs: pendingTelemetry.clientReceivedAtMs,
            pubsubSeq: pendingTelemetry.pubsubSeq,
            renderKind: "state",
            stateCommitSeq: catalogCommitSeq,
            statePhase: detail?.stateFacts.activityState,
            stateObservedAtMs: detail?.stateFacts.activityObservedAt.flatMap { LonghouseDateParser.parse($0) }
                .map { Int64($0.timeIntervalSince1970 * 1000) },
            webkit: webkitDiagnostics
        ) else { return }
        await api.postRenderBeacon(payload)
    }

    var liveActivityFingerprint: String {
        guard let detail else { return "" }
        let facts = detail.stateFacts
        let pause = detail.activePauseRequest
        var components: [String] = []
        components.reserveCapacity(20)
        components.append(detail.id)
        components.append(detail.displayTitle)
        components.append(facts.dispositionState)
        components.append(facts.runLifecycle ?? "")
        components.append(facts.activityState)
        components.append(facts.activityTool ?? "")
        components.append(facts.activityObservedAt ?? "")
        components.append(facts.controlOwnership)
        components.append(facts.controlConnection)
        components.append(facts.primary?.key ?? "")
        components.append(facts.primary?.label ?? "")
        components.append(facts.pendingInteractionKind ?? "")
        components.append(pause?.id ?? "")
        components.append(pause?.status ?? "")
        components.append(pause?.title ?? "")
        components.append(detail.project ?? "")
        components.append(detail.provider)
        return components.joined(separator: "|")
    }

    var isSessionEnded: Bool {
        guard let detail else { return false }
        return detail.isClosed
    }
}
