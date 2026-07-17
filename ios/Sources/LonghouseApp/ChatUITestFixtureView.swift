#if DEBUG
import SwiftUI
import UIKit

@MainActor
struct ChatUITestFixtureView: View {
    @EnvironmentObject private var appState: AppState
    private let fixtureName: String
    private let client: ChatUITestWorkspaceClient
    @StateObject private var viewModel: SessionViewModel
    @State private var probe: ChatUITestProbe
    @State private var invalidationTick = 0
    @State private var benchmarkStartRequested = false

    init(fixtureName: String) {
        let fixture = ChatUITestFixture(name: fixtureName)
        let sessionID: String
        if fixtureName == "benchmark-core" {
            // SessionViewModel's production cache is keyed by session ID. A unique
            // ID keeps an earlier benchmark's final transcript from becoming the
            // next run's initial state.
            let runID = UITestHooks.transcriptBenchmarkRunID ?? UUID().uuidString
            sessionID = "ui-test-transcript-benchmark-\(runID)"
        } else {
            sessionID = "ui-test-chat-session"
        }
        let client = ChatUITestWorkspaceClient(fixture: fixture, sessionID: sessionID)
        self.fixtureName = fixtureName
        self.client = client
        _probe = State(initialValue: ChatUITestProbe(path: UITestHooks.chatFixtureProbePath))
        _viewModel = StateObject(
            wrappedValue: SessionViewModel(
                apiFactory: { _ in client },
                streamFactory: { _, _, _, _ in client.streamSource() },
                enableRealtime: fixture.usesRealtimeStream
            )
        )
    }

    var body: some View {
        NavigationStack {
            SessionView(
                sessionId: client.sessionID,
                fallbackTitle: "Chat UI Fixture",
                viewModel: viewModel,
                onTranscriptDiagnostics: { diagnostics in
                    Task { @MainActor in
                        probe.record(diagnostics)
                    }
                }
            )
        }
        .overlay(alignment: .topLeading) {
            if fixtureName == "benchmark-core" {
                VStack(spacing: 0) {
                    ChatUITestProbeStatusView(probe: probe)
                        .frame(width: 2, height: 2)
                        .clipped()
                    Button {
                        benchmarkStartRequested = true
                    } label: {
                        Color.clear
                            .frame(width: 44, height: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Run transcript benchmark")
                    .accessibilityIdentifier("transcript-benchmark-start")
                }
            }
        }
        .task(id: fixtureName) {
            if fixtureName == "benchmark-core" {
                let renderer = TranscriptBenchmarkRendererKind.selected
                probe.recordBenchmarkRenderer(renderer)
                guard renderer.isImplemented else {
                    probe.recordBenchmark(phase: "renderer_unavailable", updateCount: 0)
                    return
                }
                await waitForInitialWorkspaceLoad()
                probe.recordBenchmark(phase: "ready", updateCount: 0)
                if !UITestHooks.shouldAutoStartTranscriptBenchmark {
                    while !Task.isCancelled && !benchmarkStartRequested {
                        try? await Task.sleep(nanoseconds: 25_000_000)
                    }
                }
                guard !Task.isCancelled else { return }
                let coldStalls = await MainThreadStallMonitor.shared.snapshotAndReset()
                probe.recordColdMainThreadStalls(coldStalls)
                probe.recordBenchmark(phase: "running", updateCount: 0)
                let result = await client.runTranscriptBenchmarkTrace()
                let rendered = await waitForBenchmarkRender(result.expectedLatestItemID)
                let stalls = await MainThreadStallMonitor.shared.snapshot()
                probe.recordMainThreadStalls(stalls)
                probe.recordBenchmark(
                    phase: rendered ? "complete" : "render_timeout",
                    updateCount: result.updateCount
                )
                return
            }
            if fixtureName == "render-storm" || fixtureName == "replay-file" {
                await waitForInitialWorkspaceLoad()
                await waitForParentChurnTriggerIfConfigured()
                for tick in 1...40 {
                    invalidationTick = tick
                    probe.recordTick(tick)
                    try? await Task.sleep(nanoseconds: 50_000_000)
                }
                await waitForStressTrigger()
                await client.appendAssistantMessage("Assistant fixture stress update after user scroll.")
                await viewModel.reload(sessionId: client.sessionID, appState: appState)
                return
            }
            guard fixtureName.hasPrefix("assistant-update") || fixtureName.hasPrefix("assistant-stream") else { return }
            await waitForInitialWorkspaceLoad()

            if fixtureName.hasPrefix("assistant-stream") {
                if fixtureName == "assistant-stream-latency" {
                    await waitForStressTrigger()
                } else {
                    try? await Task.sleep(nanoseconds: 1_500_000_000)
                }
                await client.streamAssistantMessage(
                    chunks: [
                        "Assistant fixture streaming",
                        "Assistant fixture streaming update",
                        "Assistant fixture streaming update at bottom.",
                    ],
                    intervalNanoseconds: 250_000_000
                )
                return
            }

            let delay: UInt64 = fixtureName == "assistant-update-keyboard"
                ? 2_500_000_000
                : 900_000_000
            let message: String
            if fixtureName == "assistant-update-keyboard" {
                message = "Assistant fixture keyboard update at bottom."
            } else if fixtureName == "assistant-update-long" {
                message = "Assistant fixture live update with wrapped tail above the floating composer card."
            } else {
                message = "Assistant fixture live update at bottom."
            }
            try? await Task.sleep(nanoseconds: delay)
            await client.appendAssistantMessage(message)
            await viewModel.reload(sessionId: client.sessionID, appState: appState)
        }
    }

    private func waitForInitialWorkspaceLoad() async {
        while !Task.isCancelled && viewModel.detail == nil {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
    }

    private func waitForStressTrigger() async {
        guard let triggerPath = UITestHooks.chatFixtureTriggerPath else { return }
        await waitForFile(at: triggerPath)
    }

    private func waitForParentChurnTriggerIfConfigured() async {
        guard let triggerPath = UITestHooks.chatFixtureChurnTriggerPath else { return }
        await waitForFile(at: triggerPath)
    }

    private func waitForFile(at path: String) async {
        while !Task.isCancelled && !FileManager.default.fileExists(atPath: path) {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
    }

    private func waitForBenchmarkRender(_ expectedLatestItemID: String) async -> Bool {
        let deadline = Date().addingTimeInterval(10)
        while !Task.isCancelled && Date() < deadline {
            if probe.latestItemID == expectedLatestItemID,
               probe.lastStage == "rendered" {
                // Require a short quiet interval so the result does not race a
                // coalesced pending render behind the final revision.
                try? await Task.sleep(nanoseconds: 250_000_000)
                return true
            }
            try? await Task.sleep(nanoseconds: 25_000_000)
        }
        return false
    }
}

@MainActor
struct TimelineOpenUITestFixtureView: View {
    @State private var probe = ChatUITestProbe(path: UITestHooks.chatFixtureProbePath)

    private let sessions: [TimelineOpenFixtureSession]

    init() {
        sessions = (1...40).map { index in
            TimelineOpenFixtureSession(
                id: "ui-test-timeline-session-\(index)",
                title: "Timeline open fixture \(index)",
                fixture: ChatUITestFixture(name: index == 1 ? "stress" : "basic")
            )
        }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(spacing: 10) {
                    ForEach(sessions) { session in
                        NavigationLink {
                            destination(for: session)
                        } label: {
                            TimelineSessionCardRow(session: session.summary, emphasized: false)
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("timeline-open-session-\(session.index)")
                    }
                }
                .padding(16)
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Timeline")
            .task {
                try? await Task.sleep(nanoseconds: 500_000_000)
                guard !Task.isCancelled else { return }
                WebTranscriptWebViewPool.prewarm()
            }
        }
    }

    private func destination(for session: TimelineOpenFixtureSession) -> some View {
        let client = ChatUITestWorkspaceClient(fixture: session.fixture, sessionID: session.id)
        let viewModel = SessionViewModel(
            apiFactory: { _ in client },
            streamFactory: { _, _, _, _ in client.streamSource() },
            enableRealtime: false
        )
        return SessionView(
            sessionId: session.id,
            fallbackTitle: session.title,
            viewModel: viewModel,
            onTranscriptDiagnostics: { diagnostics in
                Task { @MainActor in
                    probe.record(diagnostics)
                }
            }
        )
    }
}

private struct TimelineOpenFixtureSession: Identifiable {
    let id: String
    let title: String
    let fixture: ChatUITestFixture

    var index: Int {
        Int(id.split(separator: "-").last ?? "0") ?? 0
    }

    var summary: SessionSummary {
        let working = index.isMultiple(of: 3)
        let statusLabel = working ? "Working" : "Idle"
        let statusTone = working ? "running" : "inactive"
        let card = TimelineCardPresentation(
            ownership: TimelineBadgePresentation(label: "Managed", tone: "neutral"),
            status: TimelineStatusPresentation(
                label: statusLabel,
                tone: statusTone,
                seenAt: "2026-07-17T12:00:00Z",
                seenAtPrefix: "Updated"
            ),
            borderTone: statusTone
        )
        let runtime = SessionRuntimeDisplay(
            truthTier: "live",
            signalTier: "live",
            state: working ? "executing" : "quiescent",
            tone: statusTone,
            headline: statusLabel,
            detail: nil,
            phaseLabel: statusLabel,
            compactToolLabel: nil,
            isLive: working,
            isExecuting: working,
            needsAttention: false,
            isIdle: !working,
            isStalled: false,
            isManagedLocalTruth: true,
            hasSignal: true,
            controlPath: "managed",
            activityRecency: working ? "live" : "recent",
            lifecycle: "running",
            hostState: "attached",
            terminalReason: nil
        )
        return SessionSummary(
            id: id,
            title: title,
            presenceState: working ? "executing" : "quiescent",
            provider: index.isMultiple(of: 2) ? "codex" : "claude",
            project: "fixture-\(index)",
            lastActivityAt: "2026-07-17T12:00:00Z",
            summary: "Fixture transcript",
            summaryStatus: nil,
            summaryTitle: nil,
            userState: "active",
            status: nil,
            displayPhase: statusLabel,
            presenceTool: nil,
            activeTool: nil,
            gitBranch: "main",
            homeLabel: nil,
            headOriginLabel: nil,
            timelineAnchorAt: "2026-07-17T12:00:00Z",
            userMessages: index,
            toolCalls: index * 2,
            liveControlAvailable: true,
            hostReattachAvailable: false,
            replyToLiveSessionAvailable: true,
            runtimeDisplay: runtime,
            timelineCard: card
        )
    }
}

@MainActor
private final class ChatUITestProbe {
    private(set) var statusLine = ""
    private(set) var latestItemID = "none"
    private(set) var lastStage = "none"

    private let path: String?
    private var renderCount = 0
    private var duplicateCount = 0
    private var repeatRenderCount = 0
    private var lastRenderedKey: String?
    private var maxRenderDurationMs = 0
    private var renderDurationsMs: [Int] = []
    private var traceRenderDurationsMs: [Int] = []
    private var traceRenderBaseline = 0
    private var traceDuplicateBaseline = 0
    private var traceRepeatBaseline = 0
    private var coldRenderMaxMs = 0
    private var tick = 0
    private var rowCount = 0
    private var payloadBytes = 0
    private var payloadFingerprint = "none"
    private var shouldStickToBottom = false
    private var renderDurationMs = 0
    private var benchmarkPhase = "idle"
    private var benchmarkUpdateCount = 0
    private var benchmarkRenderer = "none"
    private var benchmarkSemanticTier = "none"
    private var mainThreadStallCount = 0
    private var mainThreadStallMaxMs = 0
    private var coldMainThreadStallCount = 0
    private var coldMainThreadStallMaxMs = 0
    private let buildCommit: String
    private let buildDirty: Bool
    private let deviceName: String
    private let deviceModel: String
    private let osVersion: String
    private weak var statusLabel: UILabel?

    init(path: String?) {
        self.path = path
        switch BuildIdentityLoader.loadFromMainBundle() {
        case .success(let identity):
            buildCommit = identity.commit
            buildDirty = identity.dirty
        case .failure:
            buildCommit = "unknown"
            buildDirty = true
        }
        let environment = ProcessInfo.processInfo.environment
        deviceName = environment["SIMULATOR_DEVICE_NAME"] ?? UIDevice.current.model
        deviceModel = environment["SIMULATOR_MODEL_IDENTIFIER"] ?? Self.hardwareModelIdentifier()
        osVersion = UIDevice.current.systemVersion
        rebuildAndPersist()
    }

    func record(_ diagnostics: RenderBeaconReporter.WebKitDiagnostics) {
        let latest = diagnostics.latest_item_id ?? "none"
        let fingerprint = diagnostics.payload_fingerprint ?? "\(diagnostics.row_count)|\(diagnostics.payload_byte_size)|\(latest)"
        let key = fingerprint
        if diagnostics.stage == "rendered" {
            if key == lastRenderedKey {
                repeatRenderCount += 1
            }
            lastRenderedKey = key
            renderCount += 1
            let durationMs = diagnostics.render_duration_ms ?? 0
            renderDurationsMs.append(durationMs)
            if benchmarkPhase == "running" {
                traceRenderDurationsMs.append(durationMs)
            }
            maxRenderDurationMs = max(maxRenderDurationMs, durationMs)
        } else if diagnostics.stage == "duplicate" {
            duplicateCount += 1
        }

        rowCount = diagnostics.row_count
        payloadBytes = diagnostics.payload_byte_size
        latestItemID = latest
        payloadFingerprint = fingerprint
        lastStage = diagnostics.stage
        shouldStickToBottom = diagnostics.should_stick_to_bottom
        renderDurationMs = diagnostics.render_duration_ms ?? 0
        // Avoid benchmark telemetry becoming part of the workload. The trace
        // runner observes these in-memory fields and publishes one final sample.
        if benchmarkPhase != "running" {
            rebuildAndPersist()
        }
    }

    func recordTick(_ tick: Int) {
        self.tick = tick
        rebuildAndPersist()
    }

    func recordBenchmark(phase: String, updateCount: Int) {
        if phase == "running", benchmarkPhase != "running" {
            traceRenderBaseline = renderCount
            traceDuplicateBaseline = duplicateCount
            traceRepeatBaseline = repeatRenderCount
            traceRenderDurationsMs = []
            coldRenderMaxMs = maxRenderDurationMs
        }
        benchmarkPhase = phase
        benchmarkUpdateCount = updateCount
        rebuildAndPersist()
    }

    func recordBenchmarkRenderer(_ renderer: TranscriptBenchmarkRendererKind) {
        benchmarkRenderer = renderer.rawValue
        benchmarkSemanticTier = renderer.semanticTier
        rebuildAndPersist()
    }

    func recordMainThreadStalls(_ snapshot: MainThreadStallMonitor.Snapshot) {
        mainThreadStallCount = snapshot.count
        mainThreadStallMaxMs = snapshot.maximumDurationMs
        rebuildAndPersist()
    }

    func recordColdMainThreadStalls(_ snapshot: MainThreadStallMonitor.Snapshot) {
        coldMainThreadStallCount = snapshot.count
        coldMainThreadStallMaxMs = snapshot.maximumDurationMs
        rebuildAndPersist()
    }

    func attachStatusLabel(_ label: UILabel) {
        statusLabel = label
        updateStatusLabel()
    }

    private func rebuildAndPersist() {
        statusLine = [
            "renders=\(renderCount)",
            "duplicates=\(duplicateCount)",
            "repeats=\(repeatRenderCount)",
            "rows=\(rowCount)",
            "bytes=\(payloadBytes)",
            "latest=\(latestItemID)",
            "fingerprint=\(payloadFingerprint)",
            "stage=\(lastStage)",
            "stick=\(shouldStickToBottom ? 1 : 0)",
            "render_ms=\(renderDurationMs)",
            "max_render_ms=\(maxRenderDurationMs)",
            "render_p50_ms=\(percentile(0.50))",
            "render_p95_ms=\(percentile(0.95))",
            "cold_render_max_ms=\(coldRenderMaxMs)",
            "trace_renders=\(max(0, renderCount - traceRenderBaseline))",
            "trace_duplicates=\(max(0, duplicateCount - traceDuplicateBaseline))",
            "trace_repeats=\(max(0, repeatRenderCount - traceRepeatBaseline))",
            "trace_render_p50_ms=\(percentile(0.50, samples: traceRenderDurationsMs))",
            "trace_render_p95_ms=\(percentile(0.95, samples: traceRenderDurationsMs))",
            "trace_render_max_ms=\(traceRenderDurationsMs.max() ?? 0)",
            "tick=\(tick)",
            "benchmark_phase=\(benchmarkPhase)",
            "benchmark_updates=\(benchmarkUpdateCount)",
            "benchmark_renderer=\(benchmarkRenderer)",
            "semantic_tier=\(benchmarkSemanticTier)",
            "build_commit=\(buildCommit)",
            "build_dirty=\(buildDirty ? 1 : 0)",
            "device_name=\(token(deviceName))",
            "device_model=\(token(deviceModel))",
            "os_version=\(token(osVersion))",
            "main_stalls=\(mainThreadStallCount)",
            "main_stall_max_ms=\(mainThreadStallMaxMs)",
            "cold_main_stalls=\(coldMainThreadStallCount)",
            "cold_main_stall_max_ms=\(coldMainThreadStallMaxMs)",
        ].joined(separator: " ")
        updateStatusLabel()
        persist()
    }

    private func updateStatusLabel() {
        statusLabel?.text = statusLine
        statusLabel?.accessibilityLabel = statusLine
    }

    private func token(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? "unknown"
    }

    private static func hardwareModelIdentifier() -> String {
        var systemInfo = utsname()
        uname(&systemInfo)
        return withUnsafePointer(to: &systemInfo.machine) { pointer in
            pointer.withMemoryRebound(to: CChar.self, capacity: 1) {
                String(cString: $0)
            }
        }
    }

    private func percentile(_ quantile: Double) -> Int {
        percentile(quantile, samples: renderDurationsMs)
    }

    private func percentile(_ quantile: Double, samples: [Int]) -> Int {
        guard !samples.isEmpty else { return 0 }
        let sorted = samples.sorted()
        let index = min(sorted.count - 1, Int(ceil(Double(sorted.count) * quantile)) - 1)
        return sorted[max(0, index)]
    }

    private func persist() {
        guard let path else { return }
        let url = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try? statusLine.write(to: url, atomically: true, encoding: .utf8)
    }
}

private struct ChatUITestProbeStatusView: UIViewRepresentable {
    let probe: ChatUITestProbe

    func makeUIView(context: Context) -> UILabel {
        let label = UILabel(frame: .zero)
        label.isAccessibilityElement = true
        label.accessibilityIdentifier = "transcript-benchmark-status"
        probe.attachStatusLabel(label)
        return label
    }

    func updateUIView(_ uiView: UILabel, context: Context) {
        probe.attachStatusLabel(uiView)
    }
}

private struct ChatUITestFixture: Sendable {
    let name: String
    let eventCount: Int
    let replayPath: String?

    init(name: String) {
        self.name = name
        replayPath = name == "replay-file" ? UITestHooks.chatFixtureReplayPath : nil
        let defaultCount: Int
        switch name {
        case "stress": defaultCount = 500
        case "benchmark-core": defaultCount = TranscriptBenchmarkTrace.initialRowCount
        default: defaultCount = 80
        }
        eventCount = max(0, UITestHooks.chatFixtureEventCount ?? defaultCount)
    }

    var usesRealtimeStream: Bool {
        name.hasPrefix("assistant-stream") || name == "console-reconcile" || name == "benchmark-core"
    }
}

private actor ChatUITestWorkspaceClient: SessionWorkspaceClient {
    let sessionID: String
    private let fixtureName: String
    private var nextEventID = 1
    private var events: [SessionEvent]
    private var realtimeContinuation: AsyncStream<SessionWorkspaceStream.Event>.Continuation?
    private var streamingAssistantEventID: Int?

    init(fixture: ChatUITestFixture, sessionID: String = "ui-test-chat-session") {
        self.sessionID = sessionID
        self.fixtureName = fixture.name
        var seedEvents: [SessionEvent] = []
        if let replayPath = fixture.replayPath {
            // A replay run must exercise the actual exported transcript. If the
            // file is missing/unreadable/malformed, surface it loudly rather
            // than silently falling back to synthetic data (which would let a
            // replay QA run pass without the replay).
            if let replayEvents = Self.loadReplayEvents(path: replayPath) {
                seedEvents = replayEvents
            } else {
                seedEvents = [Self.makeEvent(
                    id: 1,
                    role: "assistant",
                    content: "⚠️ Replay fixture failed to load from \(replayPath). Check LONGHOUSE_UI_TEST_CHAT_REPLAY_PATH and the export schema.",
                    timestamp: Self.fixedTimestamp(offset: 0)
                )]
            }
        } else if fixture.name == "benchmark-core" {
            seedEvents = TranscriptBenchmarkTrace.initialEvents()
        } else if fixture.name == "tools" {
            seedEvents = Self.toolFixtureEvents()
        } else if fixture.name == "marketing" {
            seedEvents = Self.marketingFixtureEvents()
        } else {
            for index in 0..<fixture.eventCount {
                let role = index.isMultiple(of: 2) ? "user" : "assistant"
                seedEvents.append(Self.makeEvent(
                    id: index + 1,
                    role: role,
                    content: Self.messageText(index: index, role: role, fixtureName: fixture.name),
                    timestamp: Self.fixedTimestamp(offset: index)
                ))
            }
        }
        events = seedEvents
        nextEventID = (seedEvents.compactMap(\.legacyNumericId).max() ?? 0) + 1
    }

    func sessionWorkspace(id: String, limit: Int, branchMode: String) async throws -> SessionWorkspaceResponse {
        Self.makeWorkspace(sessionID: sessionID, events: events, title: Self.titleForFixture(fixtureName))
    }

    func sessionMobileTail(
        id: String,
        limit: Int,
        offset: Int,
        branchMode: String,
        snapshotEventId: String?,
        cursor: String?
    ) async throws -> SessionMobileTailResponse {
        if let delayMs = UITestHooks.mobileTailDelayMs, delayMs > 0 {
            try? await Task.sleep(nanoseconds: UInt64(delayMs) * 1_000_000)
        }
        let page = fixtureName == "benchmark-core"
            ? (events: events, pageOffset: 0)
            : Self.tailPage(events: events, limit: limit, offset: offset)
        return Self.makeMobileTail(
            sessionID: sessionID,
            events: page.events,
            total: events.count,
            pageOffset: page.pageOffset,
            snapshotEventId: events.compactMap(\.legacyNumericId).max().map(String.init),
            title: Self.titleForFixture(fixtureName)
        )
    }

    func sendInput(id: String, text: String, intent: String, clientRequestId: String?) async throws -> SessionInputResponse {
        try await Task.sleep(nanoseconds: 650_000_000)
        let inputID = nextEventID
        events.append(Self.makeEvent(
            id: nextEventID,
            role: "user",
            content: text,
            timestamp: ISO8601DateFormatter().string(from: Date()),
            inputOrigin: fixtureName == "console-reconcile"
                ? nil
                : SessionInputOrigin(
                    authoredVia: .longhouse,
                    sessionInputId: inputID,
                    clientRequestId: clientRequestId
                )
        ))
        nextEventID += 1
        if fixtureName == "console-reconcile" {
            Task {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                self.appendAssistantMessage("Console fixture durable reply.")
                self.emitWorkspaceChanged()
            }
            return SessionInputResponse(
                outcome: .sent,
                inputId: nil,
                liveInputId: nil,
                clientRequestId: clientRequestId,
                turn: ConsoleTurnReceipt(turnId: "fixture-turn", runId: "fixture-run", state: "active"),
                intent: .auto,
                queued: []
            )
        }
        return SessionInputResponse(
            outcome: .sent,
            inputId: inputID,
            liveInputId: nil,
            clientRequestId: clientRequestId,
            intent: SessionInputIntent(rawValue: intent) ?? .auto,
            queued: []
        )
    }

    func sendInputMultipart(id: String, text: String, attachments: [ComposerAttachment], clientRequestId: String?) async throws -> SessionInputResponse {
        try await sendInput(id: id, text: text, intent: "auto", clientRequestId: clientRequestId)
    }

    func draftReply(id: String, maxChars: Int) async throws -> DraftReplyResponse {
        DraftReplyResponse(
            draftText: "Drafted fixture reply",
            model: "ui-test",
            generatedAt: ISO8601DateFormatter().string(from: Date()),
            basedOnEventIds: []
        )
    }

    func setSessionLoopMode(id: String, loopMode: SessionLoopMode) async throws -> LoopModeResponse {
        LoopModeResponse(sessionId: id, loopMode: loopMode)
    }

    func postRenderBeacon(_ payload: RenderBeaconReporter.Payload) async {}

    nonisolated func streamSource() -> SessionWorkspaceStreamSource {
        SessionWorkspaceStreamSource(
            start: { self.startRealtimeStream() },
            stop: { await self.stopRealtimeStream() },
            clockSkewMs: { 0 }
        )
    }

    nonisolated func startRealtimeStream() -> AsyncStream<SessionWorkspaceStream.Event> {
        AsyncStream { continuation in
            Task {
                await self.attachRealtimeContinuation(continuation)
            }
        }
    }

    nonisolated func stopRealtimeStream() async {
        await finishRealtimeStream()
    }

    private func finishRealtimeStream() {
        realtimeContinuation?.finish()
        realtimeContinuation = nil
    }

    func appendAssistantMessage(_ text: String) {
        events.append(Self.makeEvent(
            id: nextEventID,
            role: "assistant",
            content: text,
            timestamp: ISO8601DateFormatter().string(from: Date())
        ))
        nextEventID += 1
    }

    func streamAssistantMessage(chunks: [String], intervalNanoseconds: UInt64) async {
        for chunk in chunks {
            upsertStreamingAssistantMessage(chunk)
            emitWorkspaceChanged()
            try? await Task.sleep(nanoseconds: intervalNanoseconds)
        }
    }

    func runTranscriptBenchmarkTrace() async -> TranscriptBenchmarkTraceResult {
        precondition(fixtureName == "benchmark-core")
        var updateCount = 0

        for snapshot in TranscriptBenchmarkTrace.streamingSnapshots() {
            upsertStreamingAssistantMessage(snapshot)
            emitWorkspaceChanged()
            updateCount += 1
            try? await Task.sleep(nanoseconds: TranscriptBenchmarkTrace.streamingIntervalNanoseconds)
        }

        for ordinal in 1...3 {
            let callID = "benchmark-tool-\(ordinal)"
            let callEventID = nextEventID
            events.append(TranscriptBenchmarkTrace.toolCallEvent(
                id: callEventID,
                callID: callID,
                ordinal: ordinal,
                state: .running
            ))
            nextEventID += 1
            emitWorkspaceChanged()
            updateCount += 1
            try? await Task.sleep(nanoseconds: TranscriptBenchmarkTrace.streamingIntervalNanoseconds)

            if let index = events.firstIndex(where: { $0.id == String(callEventID) }) {
                events[index] = TranscriptBenchmarkTrace.toolCallEvent(
                    id: callEventID,
                    callID: callID,
                    ordinal: ordinal,
                    state: .completed
                )
            }
            events.append(TranscriptBenchmarkTrace.toolResultEvent(
                id: nextEventID,
                callID: callID,
                ordinal: ordinal
            ))
            nextEventID += 1
            emitWorkspaceChanged()
            updateCount += 1
            try? await Task.sleep(nanoseconds: TranscriptBenchmarkTrace.streamingIntervalNanoseconds)
        }

        events.insert(contentsOf: TranscriptBenchmarkTrace.olderEvents(), at: 0)
        emitWorkspaceChanged()
        updateCount += 1

        let finalID = nextEventID
        events.append(TranscriptBenchmarkTrace.messageEvent(
            id: finalID,
            role: "assistant",
            content: "Benchmark trace complete after \(updateCount) renderer updates.",
            timestampOffset: TranscriptBenchmarkTrace.initialRowCount + finalID
        ))
        nextEventID += 1
        emitWorkspaceChanged()
        updateCount += 1

        return TranscriptBenchmarkTraceResult(
            updateCount: updateCount,
            expectedLatestItemID: "prose:\(finalID)"
        )
    }

    private func attachRealtimeContinuation(
        _ continuation: AsyncStream<SessionWorkspaceStream.Event>.Continuation
    ) {
        realtimeContinuation?.finish()
        realtimeContinuation = continuation
        continuation.yield(.connected(SessionWorkspaceStream.Connected(
            session_id: sessionID,
            server_now_ms: Int64(Date().timeIntervalSince1970 * 1000)
        )))
        continuation.onTermination = { [weak self] _ in
            Task { await self?.clearRealtimeContinuation() }
        }
    }

    private func clearRealtimeContinuation() {
        realtimeContinuation = nil
    }

    private func upsertStreamingAssistantMessage(_ text: String) {
        if let eventID = streamingAssistantEventID,
           let index = events.firstIndex(where: { $0.legacyNumericId == eventID }) {
            events[index] = Self.makeEvent(
                id: eventID,
                role: "assistant",
                content: text,
                timestamp: ISO8601DateFormatter().string(from: Date())
            )
            return
        }

        let eventID = nextEventID
        streamingAssistantEventID = eventID
        events.append(Self.makeEvent(
            id: eventID,
            role: "assistant",
            content: text,
            timestamp: ISO8601DateFormatter().string(from: Date())
        ))
        nextEventID += 1
    }

    private func emitWorkspaceChanged() {
        let latestID = events.last?.legacyNumericId ?? 0
        realtimeContinuation?.yield(.changed(SessionWorkspaceStream.WorkspaceChanged(
            session_id: sessionID,
            latest_event_id: latestID,
            thread_session_count: 1,
            latest_event_emitted_at_ms: Int64(Date().timeIntervalSince1970 * 1000),
            server_fanout_at_ms: Int64(Date().timeIntervalSince1970 * 1000),
            server_now_ms: Int64(Date().timeIntervalSince1970 * 1000),
            pubsub_seq: latestID,
            transcript_preview: nil
        )))
    }

    private static func makeWorkspace(
        sessionID: String,
        events: [SessionEvent],
        title: String = "Chat UI Fixture"
    ) -> SessionWorkspaceResponse {
        let detail = makeDetail(sessionID: sessionID, events: events, title: title)
        let projectionItems = events.map { event in
            SessionProjectionItem(
                kind: "event",
                sessionId: sessionID,
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
        return SessionWorkspaceResponse(
            session: detail,
            thread: SessionThreadResponse(
                rootSessionId: sessionID,
                headSessionId: sessionID,
                sessions: [detail]
            ),
            projection: SessionProjectionResponse(
                rootSessionId: sessionID,
                focusSessionId: sessionID,
                headSessionId: sessionID,
                pathSessionIds: [sessionID],
                items: projectionItems,
                total: projectionItems.count,
                pageOffset: 0,
                branchMode: "head",
                abandonedEvents: 0
            )
        )
    }

    private static func makeMobileTail(
        sessionID: String,
        events: [SessionEvent],
        total: Int,
        pageOffset: Int,
        snapshotEventId: String?,
        title: String = "Chat UI Fixture"
    ) -> SessionMobileTailResponse {
        let detail = makeDetail(sessionID: sessionID, events: events, title: title)
        let projectionItems = events.map { event in
            SessionProjectionItem(
                kind: "event",
                sessionId: sessionID,
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
        return SessionMobileTailResponse(
            session: detail,
            projection: SessionProjectionResponse(
                rootSessionId: sessionID,
                focusSessionId: sessionID,
                headSessionId: sessionID,
                pathSessionIds: [sessionID],
                items: projectionItems,
                total: total,
                pageOffset: pageOffset,
                branchMode: "head",
                abandonedEvents: 0
            ),
            snapshotEventId: snapshotEventId
        )
    }

    private static func tailPage(events: [SessionEvent], limit: Int, offset: Int) -> (events: [SessionEvent], pageOffset: Int) {
        let total = events.count
        let pageOffset = max(0, total - limit - offset)
        let end = max(0, total - offset)
        guard pageOffset < end else {
            return ([], pageOffset)
        }
        return (Array(events[pageOffset..<end]), pageOffset)
    }

    /// Session header title for a given fixture. Marketing captures want a
    /// realistic session title, not the test-harness label.
    static func titleForFixture(_ fixtureName: String) -> String {
        fixtureName == "marketing" ? "Wire up OAuth refresh flow" : "Chat UI Fixture"
    }

    private static func makeDetail(
        sessionID: String,
        events: [SessionEvent],
        title: String = "Chat UI Fixture"
    ) -> SessionDetail {
        // Marketing captures must not leak test-harness copy into the chrome.
        let isMarketing = title == titleForFixture("marketing")
        let composerPlaceholder = isMarketing
            ? "Send a message to the live session…"
            : "Send a message to the live Codex session..."
        let idleDetail = isMarketing ? "Waiting for input" : "Waiting for UI test input"
        let available = SessionStateAction(state: "available", reason: nil)
        let unavailable = SessionStateAction(state: "unavailable", reason: "fixture_not_granted")
        let detail = SessionDetail(
            id: sessionID,
            title: title,
            provider: "codex",
            project: "longhouse",
            cwd: "/Users/example/git/zerg/longhouse",
            gitBranch: "main",
            summary: title,
            summaryTitle: title,
            presenceState: "idle",
            presenceTool: nil,
            userState: "active",
            status: "idle",
            lastActivityAt: events.last?.timestamp,
            displayPhase: "Idle",
            activeTool: nil,
            homeLabel: "MacBook",
            originLabel: "UI test",
            capabilities: SessionCapabilities(
                liveControlAvailable: true,
                hostReattachAvailable: false,
                replyToLiveSessionAvailable: true,
                canQueueNextInput: true,
                canSteerActiveTurn: false,
                displayLabel: "Send",
                displayDetail: nil,
                displayTone: "success",
                inputMode: "live",
                defaultInputIntent: "auto",
                composerEnabled: true,
                composerPlaceholder: composerPlaceholder,
                composerDisabledReason: nil,
                sendDisabledReason: nil,
                turnState: "idle",
                canStartTurn: true,
                startTurnBlockedBy: nil,
                canInterruptActiveTurn: false,
                attachImages: false,
                stalenessReason: nil
            ),
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "live",
                signalTier: "none",
                state: "idle",
                tone: "idle",
                headline: "Idle",
                detail: idleDetail,
                phaseLabel: "Idle",
                compactToolLabel: nil,
                isLive: true,
                isExecuting: false,
                needsAttention: false,
                isIdle: true,
                isStalled: false,
                isManagedLocalTruth: true,
                hasSignal: true,
                controlPath: "managed",
                activityRecency: "live",
                lifecycle: "open",
                hostState: "online",
                terminalReason: nil
            ),
            loopMode: .assist,
            stateFacts: DefaultUnknownSessionStateFacts(
                wrappedValue: SessionStateFacts(
                    contractVersion: 1,
                    presentationPolicyVersion: 1,
                    mode: "helm",
                    dispositionState: "open",
                    launchState: nil,
                    runLifecycle: "running",
                    activityState: "quiescent",
                    activityTool: nil,
                    activityObservedAt: nil,
                    activityValidUntil: nil,
                    controlOwnership: "owned",
                    controlConnection: "connected",
                    startTurn: unavailable,
                    sendInput: available,
                    interrupt: available,
                    terminate: available,
                    reattach: unavailable,
                    resume: unavailable,
                    pendingInteractionKind: nil,
                    transcriptConvergence: "current",
                    primary: SessionStateLabel(key: "idle", label: "Idle", tone: "idle", observedAt: nil),
                    access: SessionStateLabel(key: "live_control", label: "Live control", tone: "live", observedAt: nil),
                    transcript: nil
                )
            )
        )
        return detail
    }

    private static func makeEvent(
        id: Int,
        role: String,
        content: String,
        timestamp: String,
        inputOrigin: SessionInputOrigin? = nil
    ) -> SessionEvent {
        SessionEvent(
            id: id,
            role: role,
            contentText: content,
            toolName: nil,
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: nil,
            toolCallState: nil,
            timestamp: timestamp,
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: inputOrigin
        )
    }

    /// A realistic mixed transcript that exercises the redesign's demoted
    /// tool-row CSS + TimelineBuilder pairing: assistant prose, a paired
    /// tool call+result, a passive group, and a dropped (orphaned) tool call.
    private static func toolFixtureEvents() -> [SessionEvent] {
        var events: [SessionEvent] = []
        var id = 0
        func next() -> Int { id += 1; return id }
        func ts() -> String { fixedTimestamp(offset: id) }

        events.append(SessionEvent(
            id: next(), role: "user",
            contentText: "Find who renamed the ticket after the meeting and retry the MR state.",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil,
            toolCallState: nil, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "assistant",
            contentText: "Now I can see exactly what Alex did. Two new blocker tickets appeared: **PROJ-101** and **PROJ-102**. Let me pull those.",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil,
            toolCallState: nil, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        // Paired tool call + result.
        let callId = "call-jira-1"
        events.append(SessionEvent(
            id: next(), role: "assistant", contentText: nil,
            toolName: "getJiraIssue", toolInputJSON: nil, toolOutputText: nil, toolCallId: callId,
            toolCallState: .completed, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "tool", contentText: nil,
            toolName: "getJiraIssue", toolInputJSON: nil,
            toolOutputText: "PROJ-101: blocked on MR rename by Alex at 18:42.",
            toolCallId: callId, toolCallState: .completed, timestamp: ts(),
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        // A Bash call with a large-ish output (the "work is not noise" case).
        let bashId = "call-bash-1"
        events.append(SessionEvent(
            id: next(), role: "assistant", contentText: nil,
            toolName: "Bash", toolInputJSON: nil, toolOutputText: nil, toolCallId: bashId,
            toolCallState: .completed, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "tool", contentText: nil,
            toolName: "Bash", toolInputJSON: nil,
            toolOutputText: String(repeating: "git log line for changelog parsing\n", count: 12),
            toolCallId: bashId, toolCallState: .completed, timestamp: ts(),
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        // A dropped/orphaned tool call — no matching result (the trust case).
        events.append(SessionEvent(
            id: next(), role: "assistant", contentText: nil,
            toolName: "mcp__atlassian__getJiraIssue", toolInputJSON: nil, toolOutputText: nil,
            toolCallId: "call-dropped-1", toolCallState: .dropped, timestamp: ts(),
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "assistant",
            contentText: "The MR was renamed by Alex at 18:42, then moved back to In Review.",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil,
            toolCallState: nil, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        return events
    }

    /// A realistic CODING session for marketing captures: a real-feeling task
    /// (OAuth refresh), paired Read/Edit/Bash tool calls, and a clean result.
    /// On-message for the launch wedge (a coding agent you steer), unlike the
    /// Jira `tools` fixture which exists to exercise the dropped-tool case.
    private static func marketingFixtureEvents() -> [SessionEvent] {
        var events: [SessionEvent] = []
        var id = 0
        func next() -> Int { id += 1; return id }
        func ts() -> String { fixedTimestamp(offset: id) }

        events.append(SessionEvent(
            id: next(), role: "user",
            contentText: "The access token expires mid-session and users get logged out. Add silent refresh before it expires.",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil,
            toolCallState: nil, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "assistant",
            contentText: "Found it — the client only refreshes on a 401. I'll add a timer that refreshes ~60s before expiry so a request never races the token.",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil,
            toolCallState: nil, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        let readId = "call-read-1"
        events.append(SessionEvent(
            id: next(), role: "assistant", contentText: nil,
            toolName: "Read", toolInputJSON: nil, toolOutputText: nil, toolCallId: readId,
            toolCallState: .completed, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "tool", contentText: nil,
            toolName: "Read", toolInputJSON: nil,
            toolOutputText: "src/lib/auth-refresh.ts — single-flight 401 retry, no proactive refresh.",
            toolCallId: readId, toolCallState: .completed, timestamp: ts(),
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        let editId = "call-edit-1"
        events.append(SessionEvent(
            id: next(), role: "assistant", contentText: nil,
            toolName: "Edit", toolInputJSON: nil, toolOutputText: nil, toolCallId: editId,
            toolCallState: .completed, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "tool", contentText: nil,
            toolName: "Edit", toolInputJSON: nil,
            toolOutputText: "scheduleRefresh() armed on token issue; cleared on logout.",
            toolCallId: editId, toolCallState: .completed, timestamp: ts(),
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        let bashId = "call-bash-1"
        events.append(SessionEvent(
            id: next(), role: "assistant", contentText: nil,
            toolName: "Bash", toolInputJSON: nil, toolOutputText: nil, toolCallId: bashId,
            toolCallState: .completed, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "tool", contentText: nil,
            toolName: "Bash", toolInputJSON: nil,
            toolOutputText: "✓ auth.test.ts (14 passed) — refreshes 60s pre-expiry, no logout",
            toolCallId: bashId, toolCallState: .completed, timestamp: ts(),
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "assistant",
            contentText: "Done. Tokens now refresh silently a minute before expiry and tests pass. Want me to rebase onto main and open the PR?",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil,
            toolCallState: nil, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        return events
    }

    private static func loadReplayEvents(path: String) -> [SessionEvent]? {
        let url = URL(fileURLWithPath: path)
        guard let data = try? Data(contentsOf: url) else { return nil }
        do {
            let fixture = try JSONDecoder().decode(ChatUITestReplayFile.self, from: data)
            return fixture.events.enumerated().map { index, event in
                SessionEvent(
                    id: event.id ?? index + 1,
                    role: event.role,
                    contentText: event.contentText,
                    toolName: event.toolName,
                    toolInputJSON: event.toolInputJson,
                    toolOutputText: event.toolOutputText,
                    toolCallId: event.toolCallId,
                    // Real exports can't carry tool_call_state (server-derived at
                    // projection time); synthetic fixtures may set it explicitly.
                    toolCallState: event.toolCallState.flatMap(ToolCallState.init(rawValue:)),
                    timestamp: event.timestamp,
                    inActiveContext: true,
                    isHeadBranch: true,
                    inputOrigin: nil
                )
            }
        } catch {
            return nil
        }
    }

    private static func messageText(index: Int, role: String, fixtureName: String) -> String {
        if role == "assistant" {
            if fixtureName == "render-storm" {
                return """
                Assistant fixture message \(index): realistic long response with markdown, code, and enough text to stress WebKit rendering.

                - Session event id: \(index)
                - Tool summary: read, search, patch, validate
                - Runtime state: streaming

                ```swift
                struct FixtureRow\(index) {
                    let id = \(index)
                    let text = "This is a realistic transcript payload with code blocks and wrapping text."
                }
                ```

                The transcript renderer should handle this without repeatedly re-rendering identical payloads, without blocking touch scrolling, and without snapping back to the bottom when the user has intentionally scrolled upward.

                \(String(repeating: "Detailed fixture paragraph for mobile rendering, scroll anchoring, markdown layout, and text wrapping. ", count: 8))
                """
            }
            return "Assistant fixture message \(index): streaming-style response with enough body to exercise row layout."
        }
        if fixtureName == "render-storm" {
            return "User fixture message \(index): realistic request text for mobile chat stress testing, scroll anchoring, and duplicate render detection."
        }
        return "User fixture message \(index): request text for chat scroll anchoring."
    }

    private static func fixedTimestamp(offset: Int) -> String {
        let date = Date(timeIntervalSince1970: 1_777_737_600 + TimeInterval(offset))
        return ISO8601DateFormatter().string(from: date)
    }
}

private struct ChatUITestReplayFile: Decodable {
    let events: [ChatUITestReplayEvent]
}

private struct ChatUITestReplayEvent: Decodable {
    let id: Int?
    let role: String
    let contentText: String?
    let toolName: String?
    let toolInputJson: [String: JSONValue]?
    let toolOutputText: String?
    let toolCallId: String?
    let toolCallState: String?
    let timestamp: String
}
#endif
