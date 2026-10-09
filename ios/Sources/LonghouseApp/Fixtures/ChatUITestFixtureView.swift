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
    @State private var navigationPath: [String] = []
    @State private var invalidationTick = 0
    @State private var benchmarkStartRequested = false
    @State private var benchmarkScrollCompleted = false
    @State private var showBackgroundSheet = false

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
        _showBackgroundSheet = State(initialValue: fixtureName.hasPrefix("background-tasks-") && fixtureName.hasSuffix("-sheet"))
        self.client = client
        _probe = State(initialValue: ChatUITestProbe(path: UITestHooks.chatFixtureProbePath))
        // Every non-benchmark fixture shares one session ID, and the transcript
        // cache is durable. Left on the production store, whichever fixture ran
        // last becomes the next one's opening transcript — including across
        // runs, so a suite passes or fails depending on what the simulator
        // still had on disk. Give each launch its own store instead; a fixture
        // that never had a cache (no realtime stream) keeps having none. The
        // pending-input outbox is durable for the same reason, so a row one
        // fixture left unechoed would otherwise open under the next one.
        _viewModel = StateObject(
            wrappedValue: SessionViewModel(
                apiFactory: { _ in client },
                streamFactory: { _, _, _, _ in client.streamSource() },
                enableRealtime: fixture.usesRealtimeStream,
                snapshotStore: fixture.usesRealtimeStream ? Self.isolatedSnapshotStore() : nil,
                pendingInputStore: Self.isolatedPendingInputStore()
            )
        )
    }

    static func isolatedPendingInputStore() -> PendingInputStore {
        PendingInputStore(
            directory: FileManager.default.temporaryDirectory
                .appendingPathComponent("lh-ui-fixture-outbox-\(UUID().uuidString)", isDirectory: true)
        )
    }

    private static func isolatedSnapshotStore() -> TranscriptSnapshotStore {
        TranscriptSnapshotStore(
            directory: FileManager.default.temporaryDirectory
                .appendingPathComponent("lh-ui-fixture-cache-\(UUID().uuidString)", isDirectory: true)
        )
    }

    var body: some View {
        NavigationStack(path: $navigationPath) {
            SessionView(
                sessionId: client.sessionID,
                fallbackTitle: "Chat UI Fixture",
                viewModel: viewModel,
                onTranscriptDiagnostics: { diagnostics in
                    Task { @MainActor in
                        probe.record(diagnostics)
                    }
                },
                onOpenSubagent: fixtureName.hasPrefix("background-tasks")
                    ? { childSessionId in navigationPath.append(childSessionId) }
                    : nil,
                onOpenSession: fixtureName == "ended-codex-helm"
                    ? { childSessionId in navigationPath.append(childSessionId) }
                    : nil
            )
            .navigationDestination(for: String.self) { childSessionId in
                if fixtureName == "ended-codex-helm" {
                    SessionView(
                        sessionId: childSessionId,
                        fallbackTitle: "Codex Helm branch",
                        fallbackSubtitle: "cinder",
                        viewModel: SessionViewModel(
                            apiFactory: { _ in client },
                            streamFactory: { _, _, _, _ in client.streamSource() },
                            enableRealtime: false,
                            pendingInputStore: Self.isolatedPendingInputStore()
                        )
                    )
                } else {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Child transcript")
                            .font(.headline)
                        Text(childSessionId)
                            .font(.body.monospaced())
                            .accessibilityIdentifier("child-session-id")
                    }
                    .padding(24)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                    .background(Ember.page)
                    .navigationTitle("Subagent")
                    .navigationBarTitleDisplayMode(.inline)
                }
            }
        }
        .sheet(isPresented: $showBackgroundSheet) {
            SessionDelegationTaskSheet(
                facts: viewModel.detail?.stateFacts.delegation,
                asOf: Date(),
                onOpenSubagent: { childSessionId in
                    showBackgroundSheet = false
                    navigationPath.append(childSessionId)
                }
            )
        }
        .overlay(alignment: .topLeading) {
            VStack(spacing: 0) {
                // The native label is a reliable cross-process render beacon
                // even when the simulator omits WebKit's DOM accessibility
                // children. Smoke and benchmark tests both consume it.
                ChatUITestProbeStatusView(probe: probe)
                    .frame(width: 2, height: 2)
                    .clipped()
                if fixtureName == "benchmark-core" {
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
                    Button {
                        benchmarkScrollCompleted = true
                    } label: {
                        Color.clear
                            .frame(width: 44, height: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Continue transcript benchmark")
                    .accessibilityIdentifier("transcript-benchmark-continue")
                }
                if fixtureName == "background-tasks-transition" {
                    Button("Clear background work fixture") {
                        clearBackgroundDelegation()
                    }
                    .accessibilityIdentifier("background-tasks-clear")
                }
                if fixtureName == "background-completion-receipts" {
                    Button("Append assistant reply") {
                        Task {
                            let rowID = await client.appendAssistantMessage("Assistant fixture live update at bottom.")
                            await reloadUntilPublished(rowID: rowID)
                        }
                    }
                    .buttonStyle(.plain)
                    .padding(.top, 64)
                    .padding(.horizontal, 12)
                    .accessibilityLabel("Append assistant reply")
                    .accessibilityIdentifier("background-completion-append")
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
                await waitForInitialWorkspaceLoad(waitForFrame: true)
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
                let result = await client.runTranscriptBenchmarkTrace(
                    onUpdate: { revision, operation in
                        viewModel.markBenchmarkSource(revision: revision, operation: operation)
                        await viewModel.reload(sessionId: client.sessionID, appState: appState)
                    },
                    onScrollCheckpoint: { updateCount in
                        probe.recordBenchmark(phase: "scroll_ready", updateCount: updateCount)
                        while !Task.isCancelled && !benchmarkScrollCompleted {
                            try? await Task.sleep(nanoseconds: 25_000_000)
                        }
                        probe.recordBenchmark(phase: "running", updateCount: updateCount)
                    }
                )
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
                let rowID = await client.appendAssistantMessage("Assistant fixture stress update after user scroll.")
                await reloadUntilPublished(rowID: rowID)
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
            let rowID = await client.appendAssistantMessage(message)
            await reloadUntilPublished(rowID: rowID)
        }
    }

    /// `reload` joins a tail refresh that is already in flight, and that request
    /// may have read the workspace before the reply was appended. Nothing asks
    /// again, so on a slow runner the update never reached the screen
    /// (testLongAssistantUpdateKeepsWrappedTailAboveBottomChrome, run
    /// 36645544095). Reload until the row is there: 40 attempts, 250 ms apart.
    private func reloadUntilPublished(rowID: String) async {
        for _ in 0..<40 {
            await viewModel.reload(sessionId: client.sessionID, appState: appState)
            if viewModel.items.contains(where: { $0.id == rowID }) || Task.isCancelled { return }
            try? await Task.sleep(nanoseconds: 250_000_000)
        }
    }
    private func clearBackgroundDelegation() {
        guard var detail = viewModel.detail else { return }
        detail.stateFacts.delegation = SessionDelegationFacts(
            state: "none",
            count: 0,
            kinds: [:],
            source: "ui_fixture",
            observedAt: "2026-09-25T16:00:00Z",
            validUntil: "2099-01-01T00:00:00Z",
            items: []
        )
        viewModel.detail = detail
    }


    private func waitForInitialWorkspaceLoad(waitForFrame: Bool = false) async {
        // Detail now arrives in a separate primary lane. Fixture updates must
        // wait for the initial tail, not merely the title, or they can mutate
        // the workload while a cold transcript is still loading.
        while !Task.isCancelled && !viewModel.hasLoadedTranscript {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        guard waitForFrame else { return }
        // Benchmarks measure the rendered transcript, so do not start the
        // trace while WebKit is still mounting the first document.
        while !Task.isCancelled && !viewModel.isTranscriptFrameReady {
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
#endif
