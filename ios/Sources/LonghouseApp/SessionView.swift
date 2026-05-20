import SwiftUI

struct SessionWorkspaceStreamSource: Sendable {
    let start: @Sendable () async -> AsyncStream<SessionWorkspaceStream.Event>
    let stop: @Sendable () async -> Void

    static func live(baseURL: URL, sessionId: String) -> SessionWorkspaceStreamSource {
        let stream = SessionWorkspaceStream(baseURL: baseURL, sessionId: sessionId)
        return SessionWorkspaceStreamSource(
            start: { await stream.start() },
            stop: { await stream.stop() }
        )
    }
}

@MainActor
struct SessionView: View {
    let sessionId: String
    let fallbackTitle: String
    let onTranscriptDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)?

    @EnvironmentObject var appState: AppState
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var viewModel = SessionViewModel()
    @StateObject private var liveActivityManager = SessionLiveActivityManager()
    @State private var composerText: String = ""
    @FocusState private var composerFocused: Bool

    init(
        sessionId: String,
        fallbackTitle: String,
        viewModel: SessionViewModel = SessionViewModel(),
        onTranscriptDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)? = nil
    ) {
        self.sessionId = sessionId
        self.fallbackTitle = fallbackTitle
        self.onTranscriptDiagnostics = onTranscriptDiagnostics
        _viewModel = StateObject(wrappedValue: viewModel)
    }

    private var composerHasText: Bool {
        !composerText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        VStack(spacing: 0) {
            transcript
            liveActivityMessage
            runtimeDock
            composer
        }
        .navigationTitle(viewModel.detail?.displayTitle ?? fallbackTitle)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                watchButton
            }
        }
        .task(id: sessionId) { await viewModel.start(sessionId: sessionId, appState: appState) }
        .onDisappear {
            viewModel.stop()
        }
        .onChange(of: scenePhase) { _, newPhase in
            // SSE over URLSession is foreground-only per Apple's contract.
            // Tear down on background/inactive so we're not leaking a dead
            // connection; restart on return to active.
            switch newPhase {
            case .active:
                Task { await viewModel.start(sessionId: sessionId, appState: appState) }
            case .background, .inactive:
                viewModel.stop()
            @unknown default:
                break
            }
        }
        .onChange(of: viewModel.liveActivityFingerprint) { _, _ in
            guard let detail = viewModel.detail else { return }
            Task { await liveActivityManager.update(detail: detail) }
        }
        .refreshable { await viewModel.reload(sessionId: sessionId, appState: appState) }
    }

    @ViewBuilder
    private var watchButton: some View {
        if let detail = viewModel.detail {
            let isWatching = liveActivityManager.isWatching(sessionId: detail.id)
            Menu {
                Button {
                    Task { await liveActivityManager.toggle(detail: detail, appState: appState) }
                } label: {
                    Label(
                        isWatching ? "Stop Lock Screen Updates" : "Start Lock Screen Updates",
                        systemImage: isWatching ? "stop.circle" : "bell.badge"
                    )
                }
            } label: {
                if liveActivityManager.isBusy {
                    ProgressView().controlSize(.small)
                } else {
                    Label(
                        isWatching ? "Updates On" : "Updates",
                        systemImage: isWatching ? "bell.fill" : "bell"
                    )
                    .labelStyle(.titleAndIcon)
                }
            }
            .disabled(liveActivityManager.isBusy)
            .accessibilityHint("Opens Lock Screen update options")
        }
    }

    @ViewBuilder
    private var runtimeDock: some View {
        if let detail = viewModel.detail {
            SessionRuntimeDock(
                detail: detail,
                loopMode: detail.effectiveLoopMode,
                isUpdatingLoopMode: viewModel.isUpdatingLoopMode,
                onLoopModeChange: { mode in
                    Task { await viewModel.setLoopMode(sessionId: sessionId, mode: mode, appState: appState) }
                }
            )
        }
    }

    @ViewBuilder
    private var liveActivityMessage: some View {
        if let error = liveActivityManager.errorMessage {
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle")
                Text(error)
                    .font(.caption)
                Spacer(minLength: 0)
            }
            .foregroundStyle(.orange)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(.bar)
        }
    }

    private var transcript: some View {
        Group {
            if viewModel.isInitialLoading {
                ProgressView().controlSize(.large)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let error = viewModel.errorMessage, viewModel.items.isEmpty && viewModel.submittedInputs.isEmpty {
                VStack(spacing: 12) {
                    Image(systemName: "exclamationmark.triangle").foregroundStyle(.orange)
                    Text(error).multilineTextAlignment(.center).foregroundStyle(.secondary)
                    Button("Try again") {
                        Task { await viewModel.reload(sessionId: sessionId, appState: appState) }
                    }
                    .buttonStyle(.bordered)
                }
                .padding()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                WebTranscriptView(
                    items: viewModel.items,
                    submittedInputs: viewModel.submittedInputs,
                    sessionEnded: viewModel.isSessionEnded,
                    errorMessage: viewModel.errorMessage,
                    onDiagnostics: { diagnostics in
                        onTranscriptDiagnostics?(diagnostics)
                        Task {
                            await viewModel.recordTranscriptDiagnostics(
                                diagnostics,
                                sessionId: sessionId,
                                appState: appState
                            )
                        }
                    }
                )
                .accessibilityIdentifier("session-chat-transcript")
            }
        }
    }

    @ViewBuilder
    private var composer: some View {
        if let detail = viewModel.detail {
            if detail.canSendLive {
                composerField(detail: detail)
            } else {
                unavailableComposerFooter(detail: detail)
            }
        }
    }

    private func composerField(detail: SessionDetail) -> some View {
        return VStack(alignment: .leading, spacing: 6) {
            if let draftError = viewModel.draftErrorMessage {
                Text(draftError)
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            if viewModel.failedInputCount > 0 {
                Text(viewModel.failedInputCount == 1
                     ? "1 queued message failed to send."
                     : "\(viewModel.failedInputCount) queued messages failed to send.")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .accessibilityIdentifier("session-chat-queued-failed")
            }

            if viewModel.queuedInputCount > 0 {
                Text(viewModel.queuedInputCount == 1
                     ? "1 message queued — will send at next turn boundary."
                     : "\(viewModel.queuedInputCount) messages queued — will send at next turn boundary.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier("session-chat-queued-indicator")
            } else if viewModel.lastSendOutcome == .sent {
                Text("Sent.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if let draft = viewModel.turnEndedDraft {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Active turn ended")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.orange)
                    Text(draft)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                    HStack(spacing: 8) {
                        Button("Queue instead") {
                            Task { _ = await viewModel.queueInsteadOfSteer(sessionId: sessionId, appState: appState) }
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.small)
                        Button("Dismiss") {
                            viewModel.turnEndedDraft = nil
                            viewModel.errorMessage = nil
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                    }
                }
                .padding(8)
                .background(Color.orange.opacity(0.08))
                .cornerRadius(8)
                .accessibilityIdentifier("session-chat-turn-ended")
            }

            if let loopModeErrorMessage = viewModel.loopModeErrorMessage {
                Text(loopModeErrorMessage)
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            HStack(alignment: .bottom, spacing: 8) {
                // Sparkle: AI draft, only when field is empty
                Button {
                    Task { await draft() }
                } label: {
                    if viewModel.isDrafting {
                        ProgressView().controlSize(.small)
                    } else {
                        Image(systemName: "sparkles")
                            .font(.title3)
                            .foregroundStyle(composerHasText ? Color.secondary.opacity(0.3) : Color.secondary)
                    }
                }
                .frame(width: 32, height: 32)
                .disabled(composerHasText || viewModel.isSending || viewModel.isDrafting)
                .accessibilityLabel("Draft reply")

                TextField(detail.composerPlaceholder, text: $composerText, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...6)
                    .focused($composerFocused)
                    .disabled(viewModel.isDrafting)
                    .accessibilityIdentifier("session-chat-composer")

                // Send button: always a circle arrow icon; long-press reveals steer/queue split
                Button {
                    Task { await send() }
                } label: {
                    if viewModel.isSending {
                        ProgressView()
                            .frame(width: 28, height: 28)
                    } else {
                        Image(systemName: sendIcon)
                            .font(.title2)
                            .foregroundStyle(composerHasText ? Color.accentColor : Color.secondary.opacity(0.3))
                    }
                }
                .disabled(!composerHasText || viewModel.isSending || viewModel.isDrafting)
                .accessibilityLabel(sendAccessibilityLabel)
                .accessibilityIdentifier("session-chat-send")
                .contextMenu {
                    if showSecondaryQueueAction {
                        Button {
                            Task { await send(intent: "steer") }
                        } label: {
                            Label("Send update now", systemImage: "arrow.up.circle")
                        }
                        Button {
                            Task { await send(intent: "queue") }
                        } label: {
                            Label("Queue for next turn", systemImage: "clock.arrow.circlepath")
                        }
                    }
                }
            }
        }
        .padding(12)
        .background(.bar)
    }

    private func unavailableComposerFooter(detail: SessionDetail) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: detail.isControlOffline ? "wifi.slash" : "magnifyingglass")
                .foregroundStyle(detail.isControlOffline ? .orange : .secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(detail.runtimeCapabilityLabel)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.primary)
                if let message = detail.controlHealthMessage {
                    Text(message)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .background(.bar)
    }

    private var primaryIntent: String {
        guard let detail = viewModel.detail else { return "auto" }
        if detail.defaultInputIntent != "auto" { return detail.defaultInputIntent }
        guard detail.isSessionExecuting else { return "auto" }
        if detail.canSteerActiveTurn { return "steer" }
        if detail.canQueueNextInput { return "queue" }
        return "auto"
    }

    private var showSecondaryQueueAction: Bool {
        guard let detail = viewModel.detail else { return false }
        return detail.isSessionExecuting && detail.canSteerActiveTurn && detail.canQueueNextInput
    }

    private var sendIcon: String {
        switch primaryIntent {
        case "steer": return "arrow.up.circle.fill"
        case "queue": return "clock.arrow.circlepath"
        default: return "arrow.up.circle.fill"
        }
    }

    private var sendAccessibilityLabel: String {
        switch primaryIntent {
        case "steer": return "Send update mid-turn"
        case "queue": return "Queue for next turn"
        default: return "Send reply"
        }
    }

    private func send(intent: String? = nil) async {
        guard !viewModel.isSending else { return }
        let trimmed = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        let resolvedIntent = intent ?? primaryIntent
        composerText = ""
        composerFocused = false
        let sent = await viewModel.send(
            text: trimmed,
            sessionId: sessionId,
            appState: appState,
            intent: resolvedIntent,
        )
        if sent {
            let token = viewModel.sendCounter
            Task { [weak viewModel] in
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                await MainActor.run {
                    guard let vm = viewModel else { return }
                    if vm.sendCounter == token, vm.lastSendOutcome == .sent {
                        vm.lastSendOutcome = nil
                    }
                }
            }
        }
    }

    private func draft() async {
        guard let draft = await viewModel.draftReply(sessionId: sessionId, appState: appState) else { return }
        composerText = draft
        composerFocused = true
    }
}

struct SessionRuntimeDock: View {
    let detail: SessionDetail
    var loopMode: SessionLoopMode? = nil
    var isUpdatingLoopMode: Bool = false
    var onLoopModeChange: ((SessionLoopMode) -> Void)? = nil

    var body: some View {
        HStack(spacing: 10) {
            indicator
                .frame(width: 22, height: 22)

            VStack(alignment: .leading, spacing: 2) {
                Text(detail.runtimeHeadline)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                if let runtimeDetail = detail.runtimeDetail {
                    Text(runtimeDetail)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }

            Spacer(minLength: 8)

            if let loopMode, let onChange = onLoopModeChange {
                if isUpdatingLoopMode {
                    ProgressView().controlSize(.mini)
                } else {
                    LoopModeButtons(
                        currentMode: loopMode,
                        disabled: isUpdatingLoopMode,
                        onChange: onChange
                    )
                    .accessibilityIdentifier("session-loop-mode-controls")
                }
            }

            Text(capabilityLabel)
                .font(.caption2.weight(.medium))
                .foregroundStyle(capabilityColor)
                .lineLimit(1)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.bar)
        .overlay(alignment: .top) {
            Divider()
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
    }

    @ViewBuilder
    private var indicator: some View {
        if detail.isSessionExecuting {
            ProgressView()
                .controlSize(.small)
                .tint(toneColor)
        } else {
            Image(systemName: iconName)
                .font(.caption.weight(.semibold))
                .foregroundStyle(toneColor)
        }
    }

    private var capabilityLabel: String {
        detail.runtimeCapabilityLabel
    }

    private var capabilityColor: Color {
        switch detail.runtimeCapabilityTone {
        case "success": return .green
        case "warning": return .orange
        default: return .secondary
        }
    }

    private var accessibilityLabel: String {
        [detail.runtimeHeadline, detail.runtimeDetail, capabilityLabel]
            .compactMap { $0 }
            .joined(separator: ", ")
    }

    private var iconName: String {
        switch detail.runtimeTone {
        case "blocked": return "lock.circle"
        case "idle": return "checkmark.circle"
        default: return "circle"
        }
    }

    private var toneColor: Color {
        switch detail.runtimeTone {
        case "running", "thinking": return .green
        case "blocked": return .orange
        case "idle": return .gray
        default: return .gray
        }
    }
}

private struct LoopModeButtons: View {
    let currentMode: SessionLoopMode
    let disabled: Bool
    let onChange: (SessionLoopMode) -> Void

    var body: some View {
        Menu {
            Button { onChange(.assist) } label: {
                Label("Assist", systemImage: "wand.and.stars")
            }
            Button { onChange(.autopilot) } label: {
                Label("Autopilot", systemImage: "bolt.circle")
            }
            Divider()
            Button { onChange(.manual) } label: {
                Label("Off", systemImage: "pause.circle")
            }
        } label: {
            HStack(spacing: 4) {
                Image(systemName: modeIcon)
                    .font(.caption2.weight(.semibold))
                Text(modeLabel)
                    .font(.caption.weight(.medium))
                Image(systemName: "chevron.up.chevron.down")
                    .font(.caption2.weight(.semibold))
            }
            .foregroundStyle(.secondary)
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(Color(.tertiarySystemGroupedBackground), in: Capsule())
        }
        .disabled(disabled)
        .accessibilityLabel("Loop mode: \(modeLabel)")
    }

    private var modeLabel: String {
        switch currentMode {
        case .assist: return "Assist"
        case .autopilot: return "Autopilot"
        case .manual: return "Off"
        }
    }

    private var modeIcon: String {
        switch currentMode {
        case .assist: return "wand.and.stars"
        case .autopilot: return "bolt.circle"
        case .manual: return "pause.circle"
        }
    }
}

enum SubmittedInputPhase: String, Sendable {
    case submitting
    case sent
    case queued
    case failed
    case needsUserDecision
}

struct SubmittedInput: Identifiable, Sendable {
    let id: String
    let clientRequestId: String
    let text: String
    let intent: String
    var phase: SubmittedInputPhase
    var serverInputId: Int?
    var lastError: String?
    let createdAt: Date
}

// MARK: - ViewModel

@MainActor
final class SessionViewModel: ObservableObject {
    @Published var detail: SessionDetail?
    @Published var items: [TimelineItem] = []
    @Published var errorMessage: String?
    @Published var isInitialLoading = true
    @Published var isSending = false
    @Published var isDrafting = false
    @Published var isUpdatingLoopMode = false
    @Published var draftErrorMessage: String?
    @Published var loopModeErrorMessage: String?
    private var transcriptDiagnostics: RenderBeaconReporter.WebKitDiagnostics?
    /// Most recent send outcome so the UI can distinguish an immediate
    /// dispatch from a queued input without pretending the latter was sent.
    @Published var lastSendOutcome: SessionInputOutcome?
    @Published var queuedInputCount: Int = 0
    @Published var failedInputCount: Int = 0
    @Published var submittedInputs: [SubmittedInput] = []
    /// Text preserved from a steer attempt that the server rejected with
    /// error_code: "turn_ended". The UI offers an explicit "Queue instead"
    /// action; we do not silently convert the intent for the user.
    @Published var turnEndedDraft: String?
    /// Monotonic counter; each send increments it. Used so a delayed "Sent."
    /// auto-dismiss task only clears the label it owns.
    private(set) var sendCounter: UInt64 = 0

    private var pollTask: Task<Void, Never>?
    private var stream: SessionWorkspaceStreamSource?
    private var streamTask: Task<Void, Never>?
    private var streamConnected: Bool = false
    private var activeSessionId: String?
    private var lastWorkspaceEvents: [SessionEvent] = []
    private let apiFactory: (String) -> SessionWorkspaceClient?
    private let streamFactory: (URL, String) -> SessionWorkspaceStreamSource
    private let enableRealtime: Bool

    init(
        apiFactory: @escaping (String) -> SessionWorkspaceClient? = { LonghouseAPI(host: $0) },
        streamFactory: @escaping (URL, String) -> SessionWorkspaceStreamSource = { baseURL, sessionId in
            SessionWorkspaceStreamSource.live(baseURL: baseURL, sessionId: sessionId)
        },
        enableRealtime: Bool = true
    ) {
        self.apiFactory = apiFactory
        self.streamFactory = streamFactory
        self.enableRealtime = enableRealtime
    }

    func start(sessionId: String, appState: AppState) async {
        let sessionChanged = activeSessionId != sessionId
        if sessionChanged {
            activeSessionId = sessionId
            isInitialLoading = true
            detail = nil
            items = []
            submittedInputs = []
            transcriptDiagnostics = nil
            lastWorkspaceEvents = []
            errorMessage = nil
        }
        if isInitialLoading || !sessionChanged {
            await reload(sessionId: sessionId, appState: appState)
        }
        guard enableRealtime else { return }
        // Re-attach only when the session changed or the stream was torn down
        // (e.g. scenePhase != .active called stop()). Otherwise a scenePhase
        // flap would churn URLSessions and polling tasks.
        if sessionChanged || streamTask == nil {
            startStream(sessionId: sessionId, appState: appState)
        }
        if sessionChanged || pollTask == nil {
            startVisiblePolling(sessionId: sessionId, appState: appState)
        }
    }

    func stop() {
        pollTask?.cancel()
        pollTask = nil
        streamTask?.cancel()
        streamTask = nil
        Task { [stream] in await stream?.stop() }
        stream = nil
        streamConnected = false
        activeSessionId = nil
    }

    func reload(sessionId: String, appState: AppState) async {
        guard let api = apiFactory(appState.serverURL) else {
            errorMessage = "Invalid server URL"
            isInitialLoading = false
            return
        }
        do {
            try await refreshWorkspace(api: api, sessionId: sessionId)
            errorMessage = nil
            loopModeErrorMessage = nil
        } catch LonghouseAPIError.notAuthenticated {
            errorMessage = "Session expired."
        } catch {
            errorMessage = "Couldn't load session: \(error.localizedDescription)"
        }
        isInitialLoading = false
    }

    func send(text: String, sessionId: String, appState: AppState, intent: String = "auto") async -> Bool {
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
        draftErrorMessage = nil
        defer { isSending = false }
        do {
            let response = try await api.sendInput(
                id: sessionId,
                text: text,
                intent: intent,
                clientRequestId: clientRequestId
            )
            sendCounter &+= 1
            lastSendOutcome = response.outcome
            queuedInputCount = response.pendingInputCount
            failedInputCount = response.visibleFailedInputCount
            turnEndedDraft = nil
            updateSubmittedInput(
                clientRequestId,
                phase: response.outcome == .sent ? .sent : .queued,
                serverInputId: response.inputId,
                lastError: nil
            )
            clearSupersededSubmittedInputs(text: text, keepClientRequestId: clientRequestId)
            Task { [weak self] in
                guard let self else { return }
                try? await self.refreshWorkspace(api: api, sessionId: sessionId, allowFailure: true)
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
            updateSubmittedInput(
                clientRequestId,
                phase: .failed,
                serverInputId: nil,
                lastError: error.localizedDescription
            )
            errorMessage = "Send failed: \(error.localizedDescription)"
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

    func draftReply(sessionId: String, appState: AppState) async -> String? {
        guard let api = apiFactory(appState.serverURL) else { return nil }
        isDrafting = true
        draftErrorMessage = nil
        defer { isDrafting = false }
        do {
            let response = try await api.draftReply(id: sessionId, maxChars: 1200)
            let draft = response.draftText.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !draft.isEmpty else {
                draftErrorMessage = "No draft suggestion available yet."
                return nil
            }
            return draft
        } catch {
            draftErrorMessage = "Draft unavailable: \(error.localizedDescription)"
            return nil
        }
    }

    func setLoopMode(sessionId: String, mode: SessionLoopMode, appState: AppState) async {
        guard let api = apiFactory(appState.serverURL) else { return }
        isUpdatingLoopMode = true
        loopModeErrorMessage = nil
        defer { isUpdatingLoopMode = false }
        do {
            _ = try await api.setSessionLoopMode(id: sessionId, loopMode: mode)
            try await refreshWorkspace(api: api, sessionId: sessionId)
        } catch {
            loopModeErrorMessage = "Mode unavailable: \(error.localizedDescription)"
        }
    }

    func recordTranscriptDiagnostics(
        _ diagnostics: RenderBeaconReporter.WebKitDiagnostics,
        sessionId: String,
        appState: AppState
    ) async {
        transcriptDiagnostics = diagnostics
        guard diagnostics.stage == "rendered" || diagnostics.stage == "failed" else { return }
        guard let api = apiFactory(appState.serverURL) else { return }
        await reportRenderBeacon(
            api: api,
            sessionId: sessionId,
            events: lastWorkspaceEvents,
            webkitDiagnostics: diagnostics
        )
    }

    private func startVisiblePolling(sessionId: String, appState: AppState) {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                // Fallback-only: poll at 5s when SSE is disconnected. Skip when
                // SSE is live since pushes drive updates directly.
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                if Task.isCancelled { break }
                let connected = await MainActor.run { self?.streamConnected ?? false }
                if connected { continue }
                await self?.pollTick(sessionId: sessionId, appState: appState)
            }
        }
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
        let s = streamFactory(base, sessionId)
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
        case .disconnected:
            streamConnected = false
        case .heartbeat:
            break
        case .changed:
            // Push wake → refetch workspace and emit render beacon.
            guard let api = apiFactory(appState.serverURL) else { return }
            try? await refreshWorkspace(api: api, sessionId: sessionId, allowFailure: true)
        }
    }

    private func pollTick(sessionId: String, appState: AppState) async {
        guard let api = apiFactory(appState.serverURL) else { return }
        try? await refreshWorkspace(api: api, sessionId: sessionId, allowFailure: true)
    }

    private func refreshWorkspace(api: SessionWorkspaceClient, sessionId: String, allowFailure: Bool = false) async throws {
        guard activeSessionId == sessionId else { return }

        do {
            let workspace = try await api.sessionWorkspace(id: sessionId, limit: 200, branchMode: "head")
            guard activeSessionId == sessionId else { return }
            self.detail = workspace.session
            let events = workspace.events
            self.lastWorkspaceEvents = events
            self.items = TimelineBuilder.build(events: events)
            reconcileSubmittedInputs(with: events)
        } catch {
            if !allowFailure { throw error }
        }
    }

    private func updateSubmittedInput(
        _ id: String,
        phase: SubmittedInputPhase,
        serverInputId: Int?,
        lastError: String?
    ) {
        guard let index = submittedInputs.firstIndex(where: { $0.id == id }) else { return }
        submittedInputs[index].phase = phase
        submittedInputs[index].serverInputId = serverInputId
        submittedInputs[index].lastError = lastError
    }

    private func clearSupersededSubmittedInputs(text: String, keepClientRequestId: String) {
        submittedInputs.removeAll { input in
            input.clientRequestId != keepClientRequestId
                && input.text == text
                && (input.phase == .failed || input.phase == .needsUserDecision)
        }
    }

    private func reconcileSubmittedInputs(with events: [SessionEvent]) {
        guard !submittedInputs.isEmpty else { return }
        submittedInputs.removeAll { input in
            guard input.phase == .sent || input.phase == .queued || input.phase == .submitting else { return false }
            return events.contains { event in
                guard event.role == "user", event.isHeadBranch, let origin = event.inputOrigin else { return false }
                if let serverInputId = input.serverInputId,
                   origin.sessionInputId == serverInputId {
                    return true
                }
                return origin.clientRequestId == input.clientRequestId
            }
        }
    }

    private func reportRenderBeacon(
        api: SessionWorkspaceClient,
        sessionId: String,
        events: [SessionEvent],
        webkitDiagnostics: RenderBeaconReporter.WebKitDiagnostics?
    ) async {
        guard let latest = events.last else { return }
        guard let emittedAt = LonghouseDateParser.parse(latest.timestamp) else { return }
        let caps = detail?.capabilities
        let managed = (caps?.liveControlAvailable == true) || (caps?.hostReattachAvailable == true)
        if let payload = await RenderBeaconReporter.shared.payload(
            sessionId: sessionId,
            latestEventId: String(latest.id),
            emittedAt: emittedAt,
            managed: managed,
            webkit: webkitDiagnostics
        ) {
            await api.postRenderBeacon(payload)
        }
    }

    var liveActivityFingerprint: String {
        guard let detail else { return "" }
        return [
            detail.id,
            detail.displayTitle,
            detail.presenceState ?? "",
            detail.status ?? "",
            detail.presenceTool ?? "",
            detail.project ?? "",
            detail.provider,
        ].joined(separator: "|")
    }

    /// Runtime facts are authoritative when present. Unknown facts stay
    /// unknown instead of falling through to transcript-derived hints.
    var isSessionEnded: Bool {
        guard let detail else { return false }
        if let runtimeFacts = detail.runtimeFacts { return runtimeFacts.lifecycle.state == "closed" }
        if let lifecycle = detail.runtimeDisplay?.lifecycle {
            return lifecycle == "closed"
        }
        let terminal: Set<String> = ["completed", "closed", "ended", "terminated"]
        if let presence = detail.presenceState, terminal.contains(presence) { return true }
        if let status = detail.status, terminal.contains(status) { return true }
        return false
    }
}
