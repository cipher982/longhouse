import SwiftUI
import PhotosUI

struct SessionWorkspaceStreamSource: Sendable {
    let start: @Sendable () async -> AsyncStream<SessionWorkspaceStream.Event>
    let stop: @Sendable () async -> Void
    let clockSkewMs: @Sendable () async -> Int64

    static func live(baseURL: URL, sessionId: String) -> SessionWorkspaceStreamSource {
        let stream = SessionWorkspaceStream(baseURL: baseURL, sessionId: sessionId)
        return SessionWorkspaceStreamSource(
            start: { await stream.start() },
            stop: { await stream.stop() },
            clockSkewMs: { await stream.clockSkewMs() }
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
    @StateObject private var attachmentStore = ComposerAttachmentStore()
    @State private var pickerSelection: [PhotosPickerItem] = []
    @State private var isLoadingPickerItems: Bool = false

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

    private var composerHasContent: Bool {
        composerHasText || !attachmentStore.isEmpty
    }

    private var attachmentInputEnabled: Bool {
        guard viewModel.detail?.attachImagesEnabled == true else { return false }
        return primaryIntent == "auto"
    }

    private var attachmentSendBlocked: Bool {
        !attachmentStore.isEmpty && primaryIntent != "auto"
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
        let showTranscript = !viewModel.isInitialLoading
            && (!viewModel.items.isEmpty || !viewModel.submittedInputs.isEmpty || viewModel.errorMessage == nil)

        return ZStack {
            WebTranscriptView(
                items: viewModel.items,
                submittedInputs: viewModel.submittedInputs,
                sessionEnded: viewModel.isSessionEnded,
                errorMessage: viewModel.errorMessage,
                onNearTop: {
                    Task { await viewModel.loadOlder(sessionId: sessionId, appState: appState) }
                },
                onDiagnostics: { diagnostics in
                    onTranscriptDiagnostics?(diagnostics)
                    Task {
                        await viewModel.recordTranscriptDiagnostics(
                            diagnostics,
                            sessionId: sessionId,
                            appState: appState
                        )
                    }
                },
                onLifecycle: { stage in
                    viewModel.recordTranscriptLifecycle(stage)
                }
            )
            .opacity(showTranscript ? 1 : 0.01)
            .allowsHitTesting(showTranscript)
            .accessibilityHidden(!showTranscript)
            .accessibilityIdentifier("session-chat-transcript")
            .frame(maxWidth: .infinity, maxHeight: .infinity)

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

            if detail.attachImagesEnabled {
                attachmentTray
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

                if detail.attachImagesEnabled {
                    let attachmentSlotsLeft = attachmentStore.slotsLeft
                    let attachmentIsProcessing = attachmentStore.isProcessing
                    let canAttachImages = attachmentInputEnabled
                    PhotosPicker(
                        selection: $pickerSelection,
                        maxSelectionCount: max(1, attachmentSlotsLeft),
                        matching: .images
                    ) {
                        Image(systemName: attachmentIsProcessing ? "ellipsis.circle" : "paperclip")
                            .font(.title3)
                            .foregroundStyle(canAttachImages && attachmentSlotsLeft > 0 ? Color.accentColor : Color.secondary.opacity(0.3))
                    }
                    .frame(width: 32, height: 32)
                    .disabled(!canAttachImages || attachmentSlotsLeft <= 0 || attachmentIsProcessing || isLoadingPickerItems || viewModel.isSending)
                    .accessibilityLabel("Attach images")
                    .accessibilityIdentifier("session-chat-attach")
                }

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
                            .foregroundStyle(composerHasContent ? Color.accentColor : Color.secondary.opacity(0.3))
                    }
                }
                .disabled(!composerHasContent || viewModel.isSending || viewModel.isDrafting || attachmentStore.isProcessing || isLoadingPickerItems || attachmentSendBlocked)
                .accessibilityLabel(sendAccessibilityLabel)
                .accessibilityIdentifier("session-chat-send")
                .contextMenu {
                    if showSecondaryQueueAction && attachmentStore.isEmpty {
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
        .onChange(of: pickerSelection) { _, items in
            guard !items.isEmpty else { return }
            Task {
                await MainActor.run { isLoadingPickerItems = true }
                var raw: [(filename: String, data: Data)] = []
                var loadFailures = 0
                for _ in items.indices {
                    raw.append((filename: "", data: Data()))
                }
                for (idx, item) in items.enumerated() {
                    do {
                        if let data = try await item.loadTransferable(type: Data.self) {
                            raw[idx] = (filename: "image-\(UUID().uuidString).jpg", data: data)
                        } else {
                            loadFailures += 1
                        }
                    } catch {
                        loadFailures += 1
                    }
                }
                let loaded = raw.filter { !$0.data.isEmpty }
                await attachmentStore.ingest(rawImages: loaded)
                await MainActor.run {
                    if loadFailures > 0 && loaded.isEmpty {
                        attachmentStore.errorMessage = "Could not load selected image."
                    }
                    pickerSelection = []
                    isLoadingPickerItems = false
                }
            }
        }
    }

    @ViewBuilder
    private var attachmentTray: some View {
        if !attachmentStore.attachments.isEmpty || attachmentStore.errorMessage != nil {
            VStack(alignment: .leading, spacing: 6) {
                if !attachmentStore.attachments.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(attachmentStore.attachments) { item in
                                ZStack(alignment: .topTrailing) {
                                    if let thumb = item.thumbnail {
                                        Image(uiImage: thumb)
                                            .resizable()
                                            .scaledToFill()
                                            .frame(width: 56, height: 56)
                                            .clipShape(RoundedRectangle(cornerRadius: 6))
                                    } else {
                                        RoundedRectangle(cornerRadius: 6)
                                            .fill(Color.secondary.opacity(0.2))
                                            .frame(width: 56, height: 56)
                                    }
                                    Button {
                                        attachmentStore.remove(item.id)
                                    } label: {
                                        Image(systemName: "xmark.circle.fill")
                                            .font(.system(size: 18))
                                            .foregroundStyle(.white, .black.opacity(0.7))
                                            .padding(2)
                                    }
                                    .accessibilityLabel("Remove \(item.filename)")
                                }
                            }
                        }
                    }
                    .accessibilityIdentifier("session-chat-attachment-tray")
                }
                if let err = attachmentStore.errorMessage {
                    Text(err)
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .onTapGesture { attachmentStore.errorMessage = nil }
                }
            }
        }
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
        guard !attachmentStore.isProcessing else { return }
        guard !isLoadingPickerItems else { return }
        let trimmed = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
        let pendingAttachments = attachmentStore.snapshot()
        guard !trimmed.isEmpty || !pendingAttachments.isEmpty else { return }
        let requestedIntent = intent ?? primaryIntent
        if !pendingAttachments.isEmpty && requestedIntent != "auto" {
            attachmentStore.errorMessage = "Images can be sent when the session is ready for a new turn."
            return
        }
        composerText = ""
        composerFocused = false
        // Snapshot+clear before send so a slow request doesn't keep the
        // thumbnails next to a fresh empty draft.
        attachmentStore.clear()
        let sent = await viewModel.send(
            text: trimmed,
            sessionId: sessionId,
            appState: appState,
            intent: requestedIntent,
            attachments: pendingAttachments,
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
        } else if !pendingAttachments.isEmpty {
            // Send failed: re-ingest the compressed attachments so the user
            // can retry without re-picking from Photos.
            let raw = pendingAttachments.map { (filename: $0.filename, data: $0.data) }
            await attachmentStore.ingest(rawImages: raw)
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
    private struct PendingRealtimeTelemetry {
        let latestEventId: Int
        let serverFanoutAtMs: Int64?
        let clientReceivedAtMs: Int64
        let clockSkewMs: Int
        let pubsubSeq: Int?
    }

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
    private var prefetchTask: Task<Void, Never>?
    private var stream: SessionWorkspaceStreamSource?
    private var streamTask: Task<Void, Never>?
    private var streamConnected: Bool = false
    private var pendingRealtimeTelemetry: PendingRealtimeTelemetry?
    private var activeSessionId: String?
    private var activeServerURL: String?
    private var lastWorkspaceEvents: [SessionEvent] = []
    private var loadedProjectionItemCount = 0
    private var totalProjectionItemCount = 0
    private var tailSnapshotEventId: Int?
    private var prefetchedOlderTail: SessionMobileTailResponse?
    private var prefetchedOlderOffset: Int?
    private var isLoadingOlder = false
    private var openWaterfall: SessionOpenWaterfall?
    private let apiFactory: (String) -> SessionWorkspaceClient?
    private let streamFactory: (URL, String) -> SessionWorkspaceStreamSource
    private let enableRealtime: Bool
    private let transcriptCache: SessionTranscriptCache?
    private let initialTailLimit = 50
    private let olderPageLimit = 50
    private let cachedTailRefreshGraceInterval: TimeInterval = 30

    init(
        apiFactory: @escaping (String) -> SessionWorkspaceClient? = { LonghouseAPI(host: $0) },
        streamFactory: @escaping (URL, String) -> SessionWorkspaceStreamSource = { baseURL, sessionId in
            SessionWorkspaceStreamSource.live(baseURL: baseURL, sessionId: sessionId)
        },
        enableRealtime: Bool = true,
        transcriptCache: SessionTranscriptCache? = nil
    ) {
        self.apiFactory = apiFactory
        self.streamFactory = streamFactory
        self.enableRealtime = enableRealtime
        self.transcriptCache = transcriptCache ?? (enableRealtime ? .shared : nil)
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
            submittedInputs = []
            transcriptDiagnostics = nil
            pendingRealtimeTelemetry = nil
            lastWorkspaceEvents = []
            loadedProjectionItemCount = 0
            totalProjectionItemCount = 0
            tailSnapshotEventId = nil
            prefetchedOlderTail = nil
            prefetchedOlderOffset = nil
            prefetchTask?.cancel()
            prefetchTask = nil
            errorMessage = nil
            if let snapshot = transcriptCache?.snapshot(serverURL: appState.serverURL, sessionId: sessionId) {
                let ageMs = Int(Date().timeIntervalSince(snapshot.savedAt) * 1000)
                openWaterfall?.mark(
                    "cache_hit",
                    "events=\(snapshot.events.count) age_ms=\(ageMs)"
                )
                applyCachedSnapshot(snapshot)
                restoredFromCache = true
                shouldRefreshCachedTail = Date().timeIntervalSince(snapshot.savedAt) >= cachedTailRefreshGraceInterval
            } else {
                openWaterfall?.mark("cache_miss")
            }
        } else {
            activeServerURL = appState.serverURL
        }
        if isInitialLoading || !sessionChanged {
            await reload(sessionId: sessionId, appState: appState)
        } else if restoredFromCache, let api = apiFactory(appState.serverURL) {
            scheduleOlderPrefetch(api: api, sessionId: sessionId)
            if shouldRefreshCachedTail {
                Task { [weak self] in
                    try? await self?.refreshTail(api: api, sessionId: sessionId, allowFailure: true)
                }
            }
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
        openWaterfall?.mark("stop")
        openWaterfall = nil
        pollTask?.cancel()
        pollTask = nil
        prefetchTask?.cancel()
        prefetchTask = nil
        streamTask?.cancel()
        streamTask = nil
        Task { [stream] in await stream?.stop() }
        stream = nil
        streamConnected = false
        activeSessionId = nil
        activeServerURL = nil
    }

    func reload(sessionId: String, appState: AppState) async {
        guard let api = apiFactory(appState.serverURL) else {
            errorMessage = "Invalid server URL"
            isInitialLoading = false
            return
        }
        openWaterfall?.mark("reload_start")
        do {
            try await refreshTail(api: api, sessionId: sessionId)
            errorMessage = nil
            loopModeErrorMessage = nil
        } catch LonghouseAPIError.notAuthenticated {
            errorMessage = "Session expired."
        } catch {
            errorMessage = "Couldn't load session: \(error.localizedDescription)"
        }
        isInitialLoading = false
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
        draftErrorMessage = nil
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
                phase: response.outcome == .sent ? .sent : .queued,
                serverInputId: response.inputId,
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
            try await refreshTail(api: api, sessionId: sessionId)
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
        let renderMs = diagnostics.render_duration_ms.map { " render_ms=\($0)" } ?? ""
        openWaterfall?.mark(
            "webkit_\(diagnostics.stage)",
            "rows=\(diagnostics.row_count) bytes=\(diagnostics.payload_byte_size)\(renderMs)"
        )
        guard diagnostics.stage == "rendered" || diagnostics.stage == "failed" else { return }
        guard let api = apiFactory(appState.serverURL) else { return }
        await reportRenderBeacon(
            api: api,
            sessionId: sessionId,
            events: lastWorkspaceEvents,
            webkitDiagnostics: diagnostics
        )
    }

    func recordTranscriptLifecycle(_ stage: String) {
        openWaterfall?.mark(stage)
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
        case .changed(let change):
            // Push wake -> refetch the compact tail and emit render beacon.
            let nowMs = Int64(Date().timeIntervalSince1970 * 1000)
            let clockSkewMs = Int(clamping: await stream?.clockSkewMs() ?? 0)
            pendingRealtimeTelemetry = PendingRealtimeTelemetry(
                latestEventId: change.latest_event_id,
                serverFanoutAtMs: change.server_fanout_at_ms,
                clientReceivedAtMs: nowMs,
                clockSkewMs: clockSkewMs,
                pubsubSeq: change.pubsub_seq
            )
            if let transcriptPreview = change.transcript_preview?.sessionTranscriptPreview {
                applyRealtimeTranscriptPreview(transcriptPreview, sessionId: sessionId)
            }
            guard let api = apiFactory(appState.serverURL) else { return }
            try? await refreshTail(api: api, sessionId: sessionId, allowFailure: true)
        }
    }

    private func pollTick(sessionId: String, appState: AppState) async {
        guard let api = apiFactory(appState.serverURL) else { return }
        try? await refreshTail(api: api, sessionId: sessionId, allowFailure: true)
    }

    private func applyRealtimeTranscriptPreview(_ preview: SessionTranscriptPreview, sessionId: String) {
        guard activeSessionId == sessionId else { return }
        let currentDetail = detail?.replacingTranscriptPreview(preview)
        detail = currentDetail
        items = TimelineBuilder.build(
            events: TranscriptPreviewProjection.visibleEvents(
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

        if let prefetchedOlderTail, prefetchedOlderOffset == loadedProjectionItemCount {
            applyOlderTail(prefetchedOlderTail)
            self.prefetchedOlderTail = nil
            self.prefetchedOlderOffset = nil
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

        do {
            let requestStartedAt = Date()
            openWaterfall?.mark("request_start", "limit=\(initialTailLimit)")
            let tail = try await api.sessionMobileTail(
                id: sessionId,
                limit: initialTailLimit,
                offset: 0,
                branchMode: "head",
                snapshotEventId: nil
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
            self.lastWorkspaceEvents = mergedEvents
            self.loadedProjectionItemCount = min(
                tail.projection.total,
                max(0, max(tail.projection.total - tail.projection.pageOffset, mergedEvents.count))
            )
            self.totalProjectionItemCount = tail.projection.total
            self.tailSnapshotEventId = tail.snapshotEventId
            self.prefetchedOlderTail = nil
            self.prefetchedOlderOffset = nil
            let builtItems = TimelineBuilder.build(
                events: TranscriptPreviewProjection.visibleEvents(
                    durableEvents: mergedEvents,
                    preview: tail.session.transcriptPreview
                )
            )
            self.items = builtItems
            let buildMs = Int(Date().timeIntervalSince(buildStartedAt) * 1000)
            openWaterfall?.mark(
                "timeline_built",
                "events=\(mergedEvents.count) items=\(builtItems.count) elapsed_ms=\(buildMs)"
            )
            reconcileSubmittedInputs(with: mergedEvents)
            saveCurrentCache()
            scheduleOlderPrefetch(api: api, sessionId: sessionId)
        } catch {
            if !allowFailure { throw error }
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
                event.id < firstFreshTailEvent.id && !freshTailEventIds.contains(event.id)
            }
        }
        return olderEvents + freshTailEvents
    }

    private func scheduleOlderPrefetch(api: SessionWorkspaceClient, sessionId: String) {
        prefetchTask?.cancel()
        prefetchTask = nil
        guard enableRealtime else { return }
        guard activeSessionId == sessionId else { return }
        guard loadedProjectionItemCount < totalProjectionItemCount else { return }
        guard !isLoadingOlder else { return }
        let offset = loadedProjectionItemCount
        guard prefetchedOlderOffset != offset else { return }
        prefetchTask = Task { [weak self] in
            guard let self else { return }
            do {
                let tail = try await self.fetchOlderTail(api: api, sessionId: sessionId, offset: offset)
                guard self.activeSessionId == sessionId, self.loadedProjectionItemCount == offset else { return }
                self.prefetchedOlderTail = tail
                self.prefetchedOlderOffset = offset
            } catch {
                guard self.activeSessionId == sessionId, self.loadedProjectionItemCount == offset else { return }
                self.prefetchedOlderTail = nil
                self.prefetchedOlderOffset = nil
            }
            self.prefetchTask = nil
        }
    }

    private func fetchOlderTail(api: SessionWorkspaceClient, sessionId: String, offset: Int) async throws -> SessionMobileTailResponse {
        try await api.sessionMobileTail(
            id: sessionId,
            limit: olderPageLimit,
            offset: offset,
            branchMode: "head",
            snapshotEventId: tailSnapshotEventId
        )
    }

    private func applyOlderTail(_ tail: SessionMobileTailResponse) {
        totalProjectionItemCount = tail.projection.total
        loadedProjectionItemCount = max(loadedProjectionItemCount, tail.projection.total - tail.projection.pageOffset)
        let existingEventIds = Set(lastWorkspaceEvents.map(\.id))
        let olderEvents = tail.events.filter { !existingEventIds.contains($0.id) }
        if !olderEvents.isEmpty {
            lastWorkspaceEvents = olderEvents + lastWorkspaceEvents
            items = TimelineBuilder.build(
                events: TranscriptPreviewProjection.visibleEvents(
                    durableEvents: lastWorkspaceEvents,
                    preview: detail?.transcriptPreview
                )
            )
            reconcileSubmittedInputs(with: lastWorkspaceEvents)
            saveCurrentCache()
        }
    }

    private func applyCachedSnapshot(_ snapshot: SessionTranscriptCache.Snapshot) {
        detail = snapshot.detail
        lastWorkspaceEvents = snapshot.events
        loadedProjectionItemCount = snapshot.loadedProjectionItemCount
        totalProjectionItemCount = snapshot.totalProjectionItemCount
        tailSnapshotEventId = snapshot.tailSnapshotEventId
        prefetchedOlderTail = nil
        prefetchedOlderOffset = nil
        items = TimelineBuilder.build(events: snapshot.events)
        isInitialLoading = false
        errorMessage = nil
        openWaterfall?.mark("cache_applied", "events=\(snapshot.events.count) items=\(items.count)")
    }

    private func saveCurrentCache() {
        guard let transcriptCache, let activeServerURL, let activeSessionId, let detail else { return }
        transcriptCache.store(
            serverURL: activeServerURL,
            sessionId: activeSessionId,
            detail: detail.withoutTranscriptPreview,
            events: lastWorkspaceEvents,
            loadedProjectionItemCount: loadedProjectionItemCount,
            totalProjectionItemCount: totalProjectionItemCount,
            tailSnapshotEventId: tailSnapshotEventId
        )
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
        let pendingTelemetry = pendingRealtimeTelemetry
        let eventForBeacon = pendingTelemetry.flatMap { pending in
            events.last(where: { $0.id == pending.latestEventId })
        } ?? latest
        guard let emittedAt = LonghouseDateParser.parse(eventForBeacon.timestamp) else { return }
        let caps = detail?.capabilities
        let managed = (caps?.liveControlAvailable == true) || (caps?.hostReattachAvailable == true)
        let realtimeTelemetry = pendingTelemetry?.latestEventId == eventForBeacon.id
            ? pendingTelemetry
            : nil
        if let payload = await RenderBeaconReporter.shared.payload(
            sessionId: sessionId,
            latestEventId: String(eventForBeacon.id),
            emittedAt: emittedAt,
            managed: managed,
            clockSkewMs: realtimeTelemetry?.clockSkewMs ?? 0,
            serverFanoutAtMs: realtimeTelemetry?.serverFanoutAtMs,
            clientReceivedAtMs: realtimeTelemetry?.clientReceivedAtMs,
            pubsubSeq: realtimeTelemetry?.pubsubSeq,
            webkit: webkitDiagnostics
        ) {
            await api.postRenderBeacon(payload)
        }
        if pendingTelemetry != nil {
            pendingRealtimeTelemetry = nil
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

    var isSessionEnded: Bool {
        guard let detail else { return false }
        return detail.isClosed
    }
}
