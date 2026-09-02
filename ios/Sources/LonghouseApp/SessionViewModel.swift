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

    @Published var items: [TimelineItem] = [] { didSet { transcriptRevision &+= 1 } }
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
    private var prefetchedOlderOffset: Int?
    private var prefetchedOlderSnapshotEventId: String?
    private var prefetchInFlightOffset: Int?
    private var prefetchInFlightSnapshotEventId: String?
    private var prefetchInFlightToken: Int?
    private var nextPrefetchToken = 0
    private var isLoadingOlder = false
    private var openWaterfall: SessionOpenWaterfall?
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
        var restoredFromCache = false
        var shouldRefreshCachedTail = false
        if sessionChanged {
            openWaterfall = SessionOpenWaterfall(sessionId: sessionId)
            activeSessionId = sessionId
            activeServerURL = appState.serverURL
            isInitialLoading = true
            detail = nil
            items = []
            subagents = []
            submittedInputs = []
            transcriptDiagnostics = nil
            pendingRealtimeTelemetry = nil
            lastWorkspaceEvents = []
            lastWorkspaceProjectionItems = []
            loadedProjectionItemCount = 0
            totalProjectionItemCount = 0
            tailSnapshotEventId = nil
            tailNextCursor = nil
            prefetchedOlderTail = nil
            prefetchedOlderOffset = nil
            prefetchedOlderSnapshotEventId = nil
            prefetchInFlightOffset = nil
            prefetchInFlightSnapshotEventId = nil
            prefetchInFlightToken = nil
            prefetchTask?.cancel()
            prefetchTask = nil
            realtimeRefreshRetryTask?.cancel()
            realtimeRefreshRetryTask = nil
            tailRefreshTask?.cancel()
            tailRefreshTask = nil
            activeTailRefreshToken = nil
            realtimeRefreshFailureCount = 0
            errorMessage = nil
            refreshErrorMessage = nil
            pauseResponseErrorMessage = nil
            lastPubsubSeq = nil
            lastWorkspaceRevisionFingerprint = nil
            streamAuthRefreshAttempted = false
            activity.reset()
            // Warm path: the in-process tier survives backgrounding while the
            // process lives. Cold path: the durable on-disk tier survives app
            // eviction, so a relaunch into a session renders instantly instead
            // of a blank screen + lone warning triangle.
            if let restored = snapshotStore?.load(serverURL: appState.serverURL, sessionId: sessionId) {
                let ageMs = Int(Date().timeIntervalSince(restored.snapshot.savedAt) * 1000)
                openWaterfall?.mark(
                    "cache_hit",
                    "tier=\(restored.tier.rawValue) events=\(restored.snapshot.events.count) age_ms=\(ageMs)"
                )
                applySnapshot(restored)
                restoredFromCache = true
                // A snapshot is an instant paint, not the source of truth.
                // Notification opens often land seconds after new transcript
                // rows, while the SSE stream uses skip_initial=true and can miss
                // the event that caused the notification. Disk snapshots are
                // older still. Always reconcile after restoring.
                shouldRefreshCachedTail = true
            } else {
                openWaterfall?.mark("cache_miss")
            }
        } else {
            activeServerURL = appState.serverURL
        }
        let hasContentOnScreen = restoredFromCache || !items.isEmpty
        if hasContentOnScreen {
            // We already have something to show (hydrated from cache/disk, or
            // preserved across a pause). Reconcile in the background so a
            // failed refresh degrades to a banner instead of erasing the
            // transcript. This is the path that fixes the lock/unlock blank.
            if let api = apiFactory(appState.serverURL) {
                scheduleOlderPrefetch(api: api, sessionId: sessionId)
                if shouldRefreshCachedTail || !sessionChanged {
                    Task { [weak self] in
                        await self?.refreshInBackground(api: api, sessionId: sessionId)
                    }
                }
            }
            isInitialLoading = false
        } else {
            // True cold load with nothing cached: block on the fetch and show
            // a full-screen error if it fails — there is nothing to preserve.
            await reload(sessionId: sessionId, appState: appState)
        }
        guard enableRealtime else { return }
        // Re-attach only when the session changed or the stream was torn down
        // (e.g. scenePhase != .active called pauseRealtime()). Otherwise a scenePhase
        // flap would churn URLSessions and polling tasks.
        if sessionChanged || streamTask == nil {
            startStream(sessionId: sessionId, appState: appState)
        }
        if sessionChanged || pollTask == nil {
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
        pollTask?.cancel()
        pollTask = nil
        prefetchTask?.cancel()
        prefetchTask = nil
        realtimeRefreshRetryTask?.cancel()
        realtimeRefreshRetryTask = nil
        realtimeRefreshFailureCount = 0
        prefetchInFlightOffset = nil
        prefetchInFlightSnapshotEventId = nil
        prefetchInFlightToken = nil
        streamTask?.cancel()
        streamTask = nil
        Task { [stream] in await stream?.stop() }
        stream = nil
        streamConnected = false
    }

    func handleMemoryWarning() {
        let hasPrefetch = prefetchedOlderTail != nil
        openWaterfall?.mark(
            "memory_warning",
            "events=\(lastWorkspaceEvents.count) items=\(items.count) has_prefetch=\(hasPrefetch)"
        )
        prefetchTask?.cancel()
        prefetchTask = nil
        prefetchedOlderTail = nil
        prefetchedOlderOffset = nil
        prefetchedOlderSnapshotEventId = nil
        prefetchInFlightOffset = nil
        prefetchInFlightSnapshotEventId = nil
        prefetchInFlightToken = nil
    }

    /// Full teardown for genuine nav-away or session switch: stops realtime AND
    /// forgets which session was active so the next `start()` does a clean
    /// reset. Background/inactive should use `pauseRealtime()` instead.
    func stop() {
        openWaterfall?.mark("stop")
        openWaterfall = nil
        pauseRealtime()
        activeSessionId = nil
        activeServerURL = nil
    }

    func reload(sessionId: String, appState: AppState) async {
        // If we already have content on screen, a failed reload must degrade to
        // the non-destructive banner. Only a truly empty view earns the
        // full-screen blocking error.
        let hasContent = !items.isEmpty || !submittedInputs.isEmpty
        guard let api = apiFactory(appState.serverURL) else {
            if hasContent {
                refreshErrorMessage = "Invalid server URL"
            } else {
                errorMessage = "Invalid server URL"
            }
            isInitialLoading = false
            return
        }
        openWaterfall?.mark("reload_start")
        // Worker transcripts load beside the tail, never gating it: a session
        // that spawned none is the common case, and a failure here must not
        // cost the transcript.
        loadSubagents(api: api, sessionId: sessionId)
        do {
            try await refreshTail(api: api, sessionId: sessionId)
            errorMessage = nil
            refreshErrorMessage = nil
        } catch LonghouseAPIError.notAuthenticated {
            if hasContent {
                refreshErrorMessage = "Session expired. Pull to refresh."
            } else {
                errorMessage = "Session expired."
            }
        } catch {
            if hasContent {
                refreshErrorMessage = "Live update temporarily unavailable. Showing saved messages."
            } else {
                errorMessage = "Couldn't load session. Pull to refresh."
            }
        }
        isInitialLoading = false
    }

    /// Fetch this session's worker transcripts in the background.
    private func loadSubagents(api: SessionWorkspaceClient, sessionId: String) {
        Task { [weak self] in
            let response = try? await api.sessionSubagents(id: sessionId)
            await MainActor.run {
                guard let self, self.activeSessionId == sessionId else { return }
                self.subagents = response?.children ?? []
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
            createdAt: Date(),
            baselineEventIds: Set(lastWorkspaceEvents.map(\.id))
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
    static func unreadReadThrough(facts: SessionStateFacts?, sceneIsActive: Bool) -> String? {
        guard sceneIsActive, let facts, facts.unread else { return nil }
        return facts.lastResultAt
    }

    func acknowledgeUnreadIfNeeded(
        sessionId: String,
        appState: AppState,
        sceneIsActive: Bool
    ) async {
        guard let readThrough = Self.unreadReadThrough(
            facts: detail?.stateFacts,
            sceneIsActive: sceneIsActive
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

    func recordTranscriptLifecycle(_ stage: String) {
        openWaterfall?.mark(stage)
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
        guard let base = URL(string: appState.serverURL) else { return }
        // Seed the reconnect cursor from the persisted pubsub_seq so a fresh
        // stream (e.g. after a background pause) replays buffered events from
        // where we left off instead of cold. The server buffer is bounded
        // (~1000 msgs, process-local) with no gap signal, so this is a latency
        // optimization only — refreshTail() remains the correctness backstop.
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
            streamAuthRefreshAttempted = false
            openWaterfall?.mark("stream_connected")
        case .disconnected:
            streamConnected = false
            openWaterfall?.mark("stream_disconnected")
        case .unauthorized:
            streamConnected = false
            openWaterfall?.mark("stream_unauthorized")
            await handleStreamUnauthorized(sessionId: sessionId, appState: appState)
        case .replayGap(let gap):
            streamConnected = true
            openWaterfall?.mark("stream_replay_gap", "requested=\(gap.requested_seq) latest=\(gap.latest_seq)")
            if gap.session_id == sessionId {
                lastPubsubSeq = gap.latest_seq > 0 ? gap.latest_seq : nil
            }
            guard let api = apiFactory(appState.serverURL) else { return }
            await refreshTailAfterRealtimeWake(api: api, sessionId: sessionId)
        case .heartbeat:
            break
        case .changed(let change):
            activity.record(ActivityPulseStore.classify(change))
            // Push wake -> refetch the compact tail and emit render beacon.
            let nowMs = Int64(Date().timeIntervalSince1970 * 1000)
            let clockSkewMs = Int(clamping: await stream?.clockSkewMs() ?? 0)
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
            await refreshTailAfterRealtimeWake(api: api, sessionId: sessionId)
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

    private func refreshTailAfterRealtimeWake(api: SessionWorkspaceClient, sessionId: String) async {
        do {
            try await refreshTail(api: api, sessionId: sessionId)
            realtimeRefreshFailureCount = 0
            realtimeRefreshRetryTask?.cancel()
            realtimeRefreshRetryTask = nil
            refreshErrorMessage = nil
        } catch {
            scheduleRealtimeRefreshRetry(api: api, sessionId: sessionId)
        }
    }

    private func scheduleRealtimeRefreshRetry(api: SessionWorkspaceClient, sessionId: String) {
        guard activeSessionId == sessionId else { return }
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
        guard activeSessionId == sessionId else { return }
        let currentDetail = detail?.replacingTranscriptPreview(preview)
        detail = currentDetail
        items = TimelineBuilder.build(
            items: projectionItemsWithTranscriptPreview(
                lastWorkspaceProjectionItems,
                durableEvents: lastWorkspaceEvents,
                preview: currentDetail?.transcriptPreview
            )
        )
    }

    func loadOlder(sessionId: String, appState: AppState) async {
        guard activeSessionId == sessionId else { return }
        guard loadedProjectionItemCount < totalProjectionItemCount else { return }
        guard !isLoadingOlder else { return }
        guard let api = apiFactory(appState.serverURL) else { return }

        if let prefetchedOlderTail,
           prefetchedOlderOffset == loadedProjectionItemCount,
           prefetchedOlderSnapshotEventId == tailSnapshotEventId {
            applyOlderTail(prefetchedOlderTail)
            self.prefetchedOlderTail = nil
            self.prefetchedOlderOffset = nil
            self.prefetchedOlderSnapshotEventId = nil
            scheduleOlderPrefetch(api: api, sessionId: sessionId)
            return
        }

        isLoadingOlder = true
        defer { isLoadingOlder = false }
        do {
            let tail = try await fetchOlderTail(api: api, sessionId: sessionId, offset: loadedProjectionItemCount)
            guard activeSessionId == sessionId else { return }
            applyOlderTail(tail)
            scheduleOlderPrefetch(api: api, sessionId: sessionId)
        } catch let LonghouseAPIError.structured(_, code, _) where code == "projection_drift" {
            try? await refreshTail(api: api, sessionId: sessionId, allowFailure: true)
        } catch {
            // Older history is opportunistic; keep the visible tail stable.
        }
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
        activeTailRefreshToken = token
        let task = Task { [weak self] in
            guard let self else { return }
            try await self.performRefreshTail(api: api, sessionId: sessionId)
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

    private func performRefreshTail(api: SessionWorkspaceClient, sessionId: String) async throws {
        guard activeSessionId == sessionId else { return }

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
            guard activeSessionId == sessionId else { return }
            openWaterfall?.mark(
                "request_finished",
                "elapsed_ms=\(requestMs) events=\(tail.events.count) total=\(tail.projection.total)"
            )
            self.detail = tail.session
            let events = tail.events
            let buildStartedAt = Date()
            let mergedEvents = mergeRefreshedTail(events)
            let mergedProjectionItems = mergeRefreshedProjectionItems(
                freshTailItems: tail.projection.items,
                mergedEvents: mergedEvents
            )
            self.lastWorkspaceEvents = mergedEvents
            self.lastWorkspaceProjectionItems = mergedProjectionItems
            let refreshedLoadedCount = min(
                tail.projection.total,
                max(0, max(tail.projection.total - tail.projection.pageOffset, mergedEvents.count))
            )
            self.loadedProjectionItemCount = refreshedLoadedCount
            let keepPrefetchedOlderTail = prefetchedOlderOffset == refreshedLoadedCount
                && prefetchedOlderSnapshotEventId == tail.snapshotEventId
            self.totalProjectionItemCount = tail.projection.total
            self.tailSnapshotEventId = tail.snapshotEventId
            self.tailNextCursor = tail.projection.nextCursor
            self.lastWorkspaceRevisionFingerprint = tail.workspaceRevision?.fingerprint
            if !keepPrefetchedOlderTail {
                self.prefetchedOlderTail = nil
                self.prefetchedOlderOffset = nil
                self.prefetchedOlderSnapshotEventId = nil
            }
            let builtItems = TimelineBuilder.build(
                items: projectionItemsWithTranscriptPreview(
                    mergedProjectionItems,
                    durableEvents: mergedEvents,
                    preview: tail.session.transcriptPreview
                )
            )
            // Batch both mutations so SwiftUI coalesces them into one render pass,
            // preventing the one-frame duplicate where the durable event is visible
            // but the optimistic submitted input hasn't been removed yet.
            withAnimation(nil) {
                reconcileSubmittedInputs(with: mergedEvents)
                self.items = builtItems
            }
            let buildMs = Int(Date().timeIntervalSince(buildStartedAt) * 1000)
            openWaterfall?.mark(
                "timeline_built",
                "events=\(mergedEvents.count) items=\(builtItems.count) elapsed_ms=\(buildMs)"
            )
            saveCurrentCache()
            scheduleOlderPrefetch(api: api, sessionId: sessionId)
        } catch {
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
        guard loadedProjectionItemCount < totalProjectionItemCount else { return }
        guard !isLoadingOlder else { return }
        let offset = loadedProjectionItemCount
        let snapshotEventId = tailSnapshotEventId
        let hasStoredPrefetch = prefetchedOlderOffset == offset
            && prefetchedOlderSnapshotEventId == snapshotEventId
        let hasInFlightPrefetch = prefetchInFlightOffset == offset
            && prefetchInFlightSnapshotEventId == snapshotEventId
        guard !hasStoredPrefetch && !hasInFlightPrefetch else { return }

        prefetchTask?.cancel()
        prefetchTask = nil
        nextPrefetchToken += 1
        let prefetchToken = nextPrefetchToken
        prefetchInFlightOffset = offset
        prefetchInFlightSnapshotEventId = snapshotEventId
        prefetchInFlightToken = prefetchToken
        prefetchTask = Task { [weak self] in
            guard let self else { return }
            defer {
                if self.prefetchInFlightToken == prefetchToken {
                    self.prefetchInFlightOffset = nil
                    self.prefetchInFlightSnapshotEventId = nil
                    self.prefetchInFlightToken = nil
                    self.prefetchTask = nil
                }
            }
            do {
                let tail = try await self.fetchOlderTail(api: api, sessionId: sessionId, offset: offset)
                guard !Task.isCancelled,
                      self.activeSessionId == sessionId,
                      self.loadedProjectionItemCount == offset,
                      self.tailSnapshotEventId == snapshotEventId
                else { return }
                self.prefetchedOlderTail = tail
                self.prefetchedOlderOffset = offset
                self.prefetchedOlderSnapshotEventId = snapshotEventId
            } catch {
                guard !Task.isCancelled,
                      self.activeSessionId == sessionId,
                      self.loadedProjectionItemCount == offset
                else { return }
                self.prefetchedOlderTail = nil
                self.prefetchedOlderOffset = nil
                self.prefetchedOlderSnapshotEventId = nil
            }
        }
    }

    private func fetchOlderTail(api: SessionWorkspaceClient, sessionId: String, offset: Int) async throws -> SessionMobileTailResponse {
        try await api.sessionMobileTail(
            id: sessionId,
            limit: olderPageLimit,
            offset: offset,
            branchMode: "head",
            snapshotEventId: tailSnapshotEventId,
            cursor: tailNextCursor
        )
    }

    private func applyOlderTail(_ tail: SessionMobileTailResponse) {
        totalProjectionItemCount = tail.projection.total
        tailNextCursor = tail.projection.nextCursor
        lastWorkspaceRevisionFingerprint = tail.workspaceRevision?.fingerprint ?? lastWorkspaceRevisionFingerprint
        let existingItemIds = Set(lastWorkspaceProjectionItems.map(\.id))
        let olderProjectionItems = tail.projection.items.filter { !existingItemIds.contains($0.id) }
        if tail.projection.generationId != nil {
            loadedProjectionItemCount = min(
                totalProjectionItemCount,
                loadedProjectionItemCount + olderProjectionItems.count
            )
        } else {
            loadedProjectionItemCount = max(loadedProjectionItemCount, tail.projection.total - tail.projection.pageOffset)
        }
        if !olderProjectionItems.isEmpty {
            let existingEventIds = Set(lastWorkspaceEvents.map(\.id))
            let olderEvents = olderProjectionItems.compactMap(\.event).filter { !existingEventIds.contains($0.id) }
            lastWorkspaceEvents = olderEvents + lastWorkspaceEvents
            lastWorkspaceProjectionItems = olderProjectionItems + lastWorkspaceProjectionItems
            items = TimelineBuilder.build(
                items: projectionItemsWithTranscriptPreview(
                    lastWorkspaceProjectionItems,
                    durableEvents: lastWorkspaceEvents,
                    preview: detail?.transcriptPreview
                )
            )
            reconcileSubmittedInputs(with: lastWorkspaceEvents)
            saveCurrentCache()
        }
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

    private func applySnapshot(_ restored: TranscriptSnapshotStore.Restored) {
        let snapshot = restored.snapshot
        detail = snapshot.detail
        lastWorkspaceEvents = snapshot.events
        let projectionItems = snapshot.projectionItems ?? projectionItemsFromEvents(snapshot.events)
        lastWorkspaceProjectionItems = projectionItems
        loadedProjectionItemCount = snapshot.loadedProjectionItemCount
        totalProjectionItemCount = snapshot.totalProjectionItemCount
        tailSnapshotEventId = snapshot.tailSnapshotEventId
        tailNextCursor = nil
        lastPubsubSeq = snapshot.lastPubsubSeq
        lastWorkspaceRevisionFingerprint = snapshot.workspaceRevisionFingerprint
        prefetchedOlderTail = nil
        prefetchedOlderOffset = nil
        prefetchedOlderSnapshotEventId = nil
        prefetchInFlightOffset = nil
        prefetchInFlightSnapshotEventId = nil
        prefetchInFlightToken = nil
        items = TimelineBuilder.build(items: projectionItems)
        isInitialLoading = false
        errorMessage = nil
        refreshErrorMessage = nil
        openWaterfall?.mark(
            "cache_applied",
            "tier=\(restored.tier.rawValue) events=\(snapshot.events.count) items=\(items.count)"
        )
    }

    /// Background reconcile that never erases on-screen content. A failure
    /// surfaces as a thin banner (`refreshErrorMessage`); success clears it.
    private func refreshInBackground(api: SessionWorkspaceClient, sessionId: String) async {
        do {
            try await refreshTail(api: api, sessionId: sessionId)
            guard activeSessionId == sessionId else { return }
            refreshErrorMessage = nil
        } catch LonghouseAPIError.notAuthenticated {
            guard activeSessionId == sessionId else { return }
            refreshErrorMessage = "Session expired. Pull to refresh."
        } catch {
            guard activeSessionId == sessionId else { return }
            refreshErrorMessage = "Live update temporarily unavailable. Showing saved messages."
        }
        if activeSessionId == sessionId, let api = apiFactory(activeServerURL ?? "") {
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
        var matchedEventIds = Set<String>()
        submittedInputs.removeAll { input in
            guard input.phase == .sent
                || input.phase == .queued
                || input.phase == .submitting
                || input.phase == .working
                || input.phase == .couldNotConfirm
                || input.phase == .failed
            else { return false }
            if let matched = events.first(where: { event in
                guard event.role == "user", event.isHeadBranch, let origin = event.inputOrigin else { return false }
                if let serverInputId = input.serverInputId,
                   origin.sessionInputId == serverInputId {
                    return true
                }
                return origin.clientRequestId == input.clientRequestId
            }) {
                matchedEventIds.insert(matched.id)
                return true
            }

            guard input.phase == .working || input.phase == .sent else { return false }
            // Storage-v2 provider transcripts do not always retain the
            // Longhouse input receipt. Fall back to a one-to-one exact-text
            // match in a bounded time window so the durable user event can
            // replace its optimistic bubble without collapsing repeated
            // identical prompts.
            if let matched = events.first(where: { event in
                guard event.role == "user",
                      event.isHeadBranch,
                      !matchedEventIds.contains(event.id),
                      !input.baselineEventIds.contains(event.id),
                      event.contentText == input.text,
                      let eventAt = LonghouseDateParser.parse(event.timestamp)
                else { return false }
                let delta = eventAt.timeIntervalSince(input.createdAt)
                return delta >= -5 && delta <= 600
            }) {
                matchedEventIds.insert(matched.id)
                return true
            }
            return false
        }
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
