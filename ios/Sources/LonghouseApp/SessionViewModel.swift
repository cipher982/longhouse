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

    @Published var detail: SessionDetail?
    /// Viewer transport only. A connected stream is not provider liveness.
    @Published private(set) var realtimeConnection: SessionRealtimeConnection = .disconnected
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
    @Published var failedInputCount: Int = 0
    @Published var submittedInputs: [SubmittedInput] = [] { didSet { transcriptRevision &+= 1 } }
    /// Text preserved from a steer attempt that the server rejected with
    /// error_code: "turn_ended". The UI offers an explicit "Queue instead"
    /// action; we do not silently convert the intent for the user.
    @Published var turnEndedDraft: String?
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

    /// Guards against an auth-refresh→reconnect→401 loop: we attempt at most
    /// one refresh per stream session, reset once a connection succeeds.
    private var streamAuthRefreshAttempted = false
    var hasRealtimeStreamTaskForTesting: Bool { streamTask != nil }
    private var pendingRealtimeTelemetry: PendingRealtimeTelemetry?
    private var activeSessionId: String?
    private var activeServerURL: String?
    private var lastWorkspaceEvents: [SessionEvent] = []
    private var lastWorkspaceProjectionItems: [SessionProjectionItem] = []
    private var loadedProjectionItemCount = 0
    private var totalProjectionItemCount = 0
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
    private let realtimeRefreshRetryDelaysNanoseconds: [UInt64]
    private var lastPubsubSeq: Int?
    private var lastWorkspaceRevisionFingerprint: String?
    private let initialTailLimit = 50
    private let olderPageLimit = 50
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
        self.realtimeRefreshRetryDelaysNanoseconds = realtimeRefreshRetryDelaysNanoseconds
    }

    func start(sessionId: String, appState: AppState) async {
        let sessionChanged = activeSessionId != sessionId
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
            }
            transcriptReadThrough = nil
            hasLoadedTranscript = false
            activeSessionId = sessionId
            pendingTranscriptReadThrough = nil
            activeServerURL = appState.serverURL
            renderedTranscriptReadThrough = nil
            isInitialLoading = true
            isTranscriptFrameReady = false
            transcriptRendererErrorMessage = nil
            transcriptRenderRetryRevision = 0
            detail = nil
            detailWasLoadedFromTail = false
            detailWasLoadedFromPrimary = false

            items = []
            activity.reset()
            transcriptRowsReconciled = false
            subagents = []
            transcriptRowsPublishedPreview = nil
            submittedInputs = []
            loadedProjectionItemCount = 0
            totalProjectionItemCount = 0
            historyFillStalledAtLoadedCount = nil
            transcriptInputRevision &+= 1
            historyFillPendingFirstFrame = false
            transcriptDiagnostics = nil
            pendingRealtimeTelemetry = nil
            lastWorkspaceEvents = []
            lastWorkspaceProjectionItems = []
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
            subagentsRequestToken &+= 1
            subagentsRefreshPending = false

            realtimeRefreshFailureCount = 0
            errorMessage = nil
            // Fence receipts after every transcript-reset mutation, including
            // the final blocking-error clear above.
            transcriptRevisionFloor = transcriptRevision
            refreshErrorMessage = nil
            pauseResponseErrorMessage = nil
            lastPubsubSeq = nil
            lastWorkspaceRevisionFingerprint = nil
            streamAuthRefreshAttempted = false
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
            activeServerURL = appState.serverURL
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
                        self.detail = loaded.preservingOptionalEnrichment(from: existing)
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

    func startBranch(sessionId: String, appState: AppState) async {
        let text = branchMessage.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isBranching else { return }
        guard let api = apiFactory(appState.serverURL) else {
            branchErrorMessage = "The Longhouse server URL is invalid."
            return
        }
        isBranching = true
        branchErrorMessage = nil
        defer { isBranching = false }
        do {
            let branch = try await api.createSessionBranch(
                id: sessionId,
                message: text,
                clientRequestId: UUID().uuidString
            )
            // Only clear the draft once the branch exists. Losing what someone
            // typed is the worst possible answer to a failure they can retry.
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

    func send(
        text: String,
        sessionId: String,
        appState: AppState,
        intent: String = "auto",
        attachments: [ComposerAttachment] = []
    ) async -> Bool {
        let clientRequestId = "ios-\(UUID().uuidString)"
        let localInput = SubmittedInput(
            id: clientRequestId,
            clientRequestId: clientRequestId,
            text: text,
            intent: intent,
            phase: .submitting,
            serverInputId: nil,
            lastError: nil,
            createdAt: Date()
        )
        submittedInputs.append(localInput)
        guard let api = apiFactory(appState.serverURL) else {
            updateSubmittedInput(
                clientRequestId,
                phase: .failed,
                serverInputId: nil,
                lastError: "Invalid server URL"
            )
            return false
        }
        isSending = true
        defer { isSending = false }
        do {
            let response: SessionInputResponse
            if attachments.isEmpty {
                response = try await api.sendInput(
                    id: sessionId,
                    text: text,
                    intent: intent,
                    clientRequestId: clientRequestId
                )
            } else {
                // Server v1 multipart accepts intent=auto only; the UI gates
                // attachments to managed Codex sessions at the composer level.
                response = try await api.sendInputMultipart(
                    id: sessionId,
                    text: text,
                    attachments: attachments,
                    clientRequestId: clientRequestId
                )
            }
            sendCounter &+= 1
            lastSendOutcome = response.outcome
            queuedInputCount = response.pendingInputCount
            failedInputCount = response.visibleFailedInputCount
            turnEndedDraft = nil
            updateSubmittedInput(
                clientRequestId,
                phase: response.turn.map { ["starting", "active", "draining"].contains($0.state) } == true
                    ? .working
                    : (response.outcome == .sent ? .sent : .queued),
                serverInputId: response.inputId,
                turnId: response.turn?.turnId,
                runId: response.turn?.runId,
                lastError: nil
            )
            clearSupersededSubmittedInputs(text: text, keepClientRequestId: clientRequestId)
            Task { [weak self] in
                guard let self else { return }
                try? await self.refreshTail(api: api, sessionId: sessionId, allowFailure: true)
            }
            return true
        } catch let LonghouseAPIError.structured(_, code, message) where intent == "steer" && code == "turn_ended" {
            // Preserve the original text; the UI offers an explicit
            // "Queue instead" action. Intent is never silently mapped.
            updateSubmittedInput(
                clientRequestId,
                phase: .needsUserDecision,
                serverInputId: nil,
                lastError: message.isEmpty ? "Active turn ended before your update arrived." : message
            )
            turnEndedDraft = text
            errorMessage = message.isEmpty ? "Active turn ended before your update arrived." : message
            return false
        } catch {
            let failureMessage = sendFailureMessage(for: error)
            if sendConfirmationMayHaveLanded(error) {
                updateSubmittedInput(
                    clientRequestId,
                    phase: .couldNotConfirm,
                    serverInputId: nil,
                    lastError: failureMessage
                )
                errorMessage = nil
                refreshErrorMessage = failureMessage
                Task { [weak self] in
                    guard let self else { return }
                    try? await self.refreshTail(api: api, sessionId: sessionId, allowFailure: true)
                }
                return false
            }
            updateSubmittedInput(
                clientRequestId,
                phase: .failed,
                serverInputId: nil,
                lastError: failureMessage
            )
            errorMessage = "Could not send: \(failureMessage)"
            Task { [weak self] in
                guard let self else { return }
                try? await self.refreshTail(api: api, sessionId: sessionId, allowFailure: true)
            }
            return false
        }
    }

    /// Explicit user acceptance of the "Queue instead" prompt after a
    /// steer failed with turn_ended. Always maps to intent=queue.
    func queueInsteadOfSteer(sessionId: String, appState: AppState) async -> Bool {
        guard let text = turnEndedDraft else { return false }
        let decisionIds = submittedInputs
            .filter { $0.phase == .needsUserDecision && $0.text == text }
            .map(\.id)
        let queued = await send(text: text, sessionId: sessionId, appState: appState, intent: "queue")
        if queued {
            turnEndedDraft = nil
            submittedInputs.removeAll { decisionIds.contains($0.id) }
        }
        return queued
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
                let (connected, hasRunningTool, setupPending, stillHasPendingInput) = await MainActor.run {
                    (
                        self.streamConnected,
                        self.lastWorkspaceEvents.contains { $0.toolCallState == .running },
                        self.detail?.canDraftBeforeSendReady == true,
                        Self.pendingInputPollDelay(submittedInputs: self.submittedInputs, now: Date()) != nil
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
                    ticks: ticks
                ) {
                    self.openWaterfall?.mark(
                        "poll_tail",
                        "connected=\(connected) setup_pending=\(setupPending) pending_input=\(stillHasPendingInput) running_tool=\(hasRunningTool) tick=\(ticks)"
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
        if hasRunningTool, ticks % 12 == 0 { return true }
        _ = managed
        return false
    }

    static func visiblePollDelayNanoseconds(completedTicks: Int) -> UInt64 {
        completedTicks < 3 ? 750_000_000 : 5_000_000_000
    }

    static func pendingInputPollDelay(submittedInputs: [SubmittedInput], now: Date) -> UInt64? {
        let activeAges = submittedInputs.compactMap { input -> TimeInterval? in
            guard input.phase == .submitting || input.phase == .working || input.phase == .sent else { return nil }
            return max(0, now.timeIntervalSince(input.createdAt))
        }
        guard let youngest = activeAges.min(), youngest <= 120 else { return nil }
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
        // Seed the reconnect cursor from the persisted pubsub_seq so a fresh
        // stream (e.g. after a background pause) replays buffered events from
        // where we left off instead of cold. The server buffer is bounded
        // (~1000 msgs, process-local) with no gap signal, so this is a latency
        // optimization only — refreshTail() remains the correctness backstop.
        openWaterfall?.mark(
            "stream_start",
            "since_seq=\(lastPubsubSeq ?? 0) known_fingerprint=\(lastWorkspaceRevisionFingerprint != nil)"
        )
        let s = streamFactory(base, sessionId, lastPubsubSeq, lastWorkspaceRevisionFingerprint)
        stream = s
        streamTask = Task { [weak self] in
            let events = await s.start()
            for await event in events {
                if Task.isCancelled { break }
                await self?.handleStreamEvent(event, sessionId: sessionId, appState: appState)
            }
        }
    }

    private func handleStreamEvent(_ event: SessionWorkspaceStream.Event, sessionId: String, appState: AppState) async {
        switch event {
        case .connected:
            streamConnected = true
            realtimeConnection = .connected
            streamAuthRefreshAttempted = false
            openWaterfall?.mark("stream_connected")
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
            openWaterfall?.mark("stream_replay_gap", "requested=\(gap.requested_seq) latest=\(gap.latest_seq)")
            if gap.session_id == sessionId {
                lastPubsubSeq = gap.latest_seq > 0 ? gap.latest_seq : nil
                lastWorkspaceRevisionFingerprint = nil
            }
            guard let api = apiFactory(appState.serverURL) else { return }
            requestRealtimeRefresh(api: api, sessionId: sessionId)
        case .heartbeat:
            break
        case .changed(let change):
            if let kind = ActivityPulseStore.classify(change) {
                activity.record(kind)
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
            switch change.change_kind {
            case "runtime", "title_update", "read_update":
                // These wakes change native chrome, not transcript rows.
                // Refresh the compact detail lane and leave the expensive
                // mobile-tail/timeline/WebKit path untouched.
                loadPrimaryDetail(api: api, sessionId: sessionId)
            default:
                requestRealtimeRefresh(api: api, sessionId: sessionId)
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
        try? await refreshTail(api: api, sessionId: sessionId, allowFailure: true)
    }

    /// One durable refresh per burst of wakes. A catalog commit fans out as
    /// several frames within milliseconds; refreshing once per frame turned a
    /// burst of N into N sequential tail reads and N WebKit renders. While a
    /// refresh is in flight the newest wake only marks it dirty, and at most
    /// one follow-up runs when it lands.
    private func requestRealtimeRefresh(api: SessionWorkspaceClient, sessionId: String) {
        if realtimeRefreshTask != nil {
            realtimeRefreshPending = true
            return
        }
        // Joining an older request is not enough: that request may have
        // captured its snapshot before this wake. Force one coalesced
        // post-request fetch so the invalidation cannot be lost.
        if tailRefreshTask != nil {
            realtimeRefreshPending = true
        }
        realtimeRefreshRequestToken &+= 1
        let requestToken = realtimeRefreshRequestToken
        realtimeRefreshTask = Task { [weak self] in
            guard let self else { return }
            repeat {
                self.realtimeRefreshPending = false
                await self.refreshTailAfterRealtimeWake(api: api, sessionId: sessionId)
            } while self.realtimeRefreshPending
                && self.activeSessionId == sessionId
                && self.realtimeRefreshRequestToken == requestToken
                && !Task.isCancelled
            if self.realtimeRefreshRequestToken == requestToken {
                self.realtimeRefreshTask = nil
            }
        }
    }

    private func refreshTailAfterRealtimeWake(api: SessionWorkspaceClient, sessionId: String) async {
        guard activeSessionId == sessionId, !realtimePaused else { return }
        let generation = routeLoadGeneration
        let joinedExistingTail = tailRefreshTask != nil
        do {
            try await refreshTail(api: api, sessionId: sessionId)
            guard isCurrentRoute(sessionId: sessionId, generation: generation) else { return }
            // A wake that joined a pre-existing request still needs one
            // post-wake fetch: that request may have captured its snapshot
            // before the invalidation arrived.
            if joinedExistingTail {
                realtimeRefreshPending = true
            }
            realtimeRefreshFailureCount = 0
            realtimeRefreshRetryTask?.cancel()
            realtimeRefreshRetryTask = nil
            refreshErrorMessage = nil
        } catch is CancellationError {
            return
        } catch {
            guard isCurrentRoute(sessionId: sessionId, generation: generation), !realtimePaused else { return }
            scheduleRealtimeRefreshRetry(api: api, sessionId: sessionId)
        }
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
            await self?.refreshTailAfterRealtimeWake(api: api, sessionId: sessionId)
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
        var pending = pendingRealtimePreviewBuild
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
    func loadOlder(sessionId: String, appState: AppState) async {
        guard let api = apiFactory(appState.serverURL) else { return }
        await loadOlder(api: api, sessionId: sessionId)
    }


    /// WebKit measured the rendered transcript shorter than its viewport: a
    /// 50-event window grouped down to a few rows. Pull one older page; the
    /// next render measures again, so this repeats until the viewport fills,
    /// history runs out, or a page adds nothing.
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
            try await self.performRefreshTail(
                api: api,
                sessionId: sessionId,
                generation: generation
            )
        }
        tailRefreshTask = task
        defer {
            if activeTailRefreshToken == token {
                tailRefreshTask = nil
                activeTailRefreshToken = nil
            }
        }

        do {
            try await task.value
        } catch {
            if !allowFailure { throw error }
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
            let tail = try await api.sessionMobileTail(
                id: sessionId,
                limit: initialTailLimit,
                offset: 0,
                branchMode: "head",
                snapshotEventId: nil,
                cursor: nil
            )
            let requestMs = Int(Date().timeIntervalSince(requestStartedAt) * 1000)
            guard isCurrentRoute(sessionId: sessionId, generation: generation) else {
                throw CancellationError()
            }
            openWaterfall?.mark(
                "request_finished",
                "elapsed_ms=\(requestMs) events=\(tail.events.count) total=\(tail.projection.total)"
            )
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
            let mergedEvents = mergeRefreshedTail(tail.events)
            let mergedProjectionItems = mergeRefreshedProjectionItems(
                freshTailItems: tail.projection.items,
                mergedEvents: mergedEvents
            )
            let refreshedLoadedCount = min(
                tail.projection.total,
                max(0, max(tail.projection.total - tail.projection.pageOffset, mergedProjectionItems.count))
            )
            let keepPrefetchedOlderTail = prefetchedOlderCursor == tail.projection.nextCursor
                && prefetchedOlderSnapshotEventId == tail.snapshotEventId
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
                tailSnapshotEventId = tail.snapshotEventId
                tailNextCursor = tail.projection.nextCursor
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

        let blockingLoadError = errorMessage
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
                workspaceRevisionFingerprint: lastWorkspaceRevisionFingerprint
            )
        )
    }

    private func updateSubmittedInput(
        _ id: String,
        phase: SubmittedInputPhase,
        serverInputId: Int?,
        turnId: String? = nil,
        runId: String? = nil,
        lastError: String?
    ) {
        guard let index = submittedInputs.firstIndex(where: { $0.id == id }) else { return }
        submittedInputs[index].phase = phase
        submittedInputs[index].serverInputId = serverInputId
        if let turnId { submittedInputs[index].turnId = turnId }
        if let runId { submittedInputs[index].runId = runId }
        submittedInputs[index].lastError = lastError
    }

    private func clearSupersededSubmittedInputs(text: String, keepClientRequestId: String) {
        submittedInputs.removeAll { input in
            input.clientRequestId != keepClientRequestId
                && input.text == text
                && (input.phase == .failed || input.phase == .couldNotConfirm || input.phase == .needsUserDecision)
        }
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

    /// An optimistic send row is resolved by identity only. The server links
    /// each delivered receipt to the durable user event it became at ingest,
    /// so the receipt says the echo exists even when a long turn has already
    /// pushed that event out of the loaded tail. A loaded event stamped with
    /// the same origin is the same fact seen from the page. Text and time
    /// never decide: repeated identical prompts and abandoned drafts made
    /// that guess wrong in both directions.
    static func resolvedSubmittedInputIds(
        submittedInputs: [SubmittedInput],
        events: [SessionEvent],
        receipts: [SessionInputReceipt]
    ) -> Set<String> {
        let linkedRequestIds = Set(receipts.compactMap { $0.eventId == nil ? nil : $0.clientRequestId })
        var resolved = Set<String>()
        for input in submittedInputs {
            guard input.phase == .sent
                || input.phase == .queued
                || input.phase == .submitting
                || input.phase == .working
                || input.phase == .couldNotConfirm
                || input.phase == .failed
            else { continue }
            if linkedRequestIds.contains(input.clientRequestId) {
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

    private func sendConfirmationMayHaveLanded(_ error: Error) -> Bool {
        switch error {
        case LonghouseAPIError.upstreamFailed,
             LonghouseAPIError.requestFailed,
             LonghouseAPIError.unexpectedResponse,
             LonghouseAPIError.serviceUnavailable:
            return true
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
        return [
            detail.id,
            detail.displayTitle,
            facts.dispositionState,
            facts.runLifecycle ?? "",
            facts.activityState,
            facts.activityTool ?? "",
            facts.activityObservedAt ?? "",
            facts.controlOwnership,
            facts.controlConnection,
            facts.primary?.key ?? "",
            facts.primary?.label ?? "",
            facts.pendingInteractionKind ?? "",
            pause?.id ?? "",
            pause?.status ?? "",
            pause?.title ?? "",
            detail.project ?? "",
            detail.provider,
        ].joined(separator: "|")
    }

    var isSessionEnded: Bool {
        guard let detail else { return false }
        return detail.isClosed
    }
}
