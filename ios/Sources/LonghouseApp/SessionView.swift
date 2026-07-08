import SwiftUI
import PhotosUI
import UIKit

struct SessionWorkspaceStreamSource: Sendable {
    let start: @Sendable () async -> AsyncStream<SessionWorkspaceStream.Event>
    let stop: @Sendable () async -> Void
    let clockSkewMs: @Sendable () async -> Int64

    static func live(
        baseURL: URL,
        sessionId: String,
        sinceSeq: Int? = nil,
        knownWorkspaceFingerprint: String? = nil
    ) -> SessionWorkspaceStreamSource {
        let stream = SessionWorkspaceStream(
            baseURL: baseURL,
            sessionId: sessionId,
            sinceSeq: sinceSeq,
            knownWorkspaceFingerprint: knownWorkspaceFingerprint
        )
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
    @State private var isShowingPhotoPicker: Bool = false
    @State private var isLoadingPickerItems: Bool = false
    /// Breathing room (pt) above the floating control card, added on top of the
    /// SwiftUI-computed bottom safe area so the last transcript row never butts
    /// directly against the chrome.
    private static let transcriptComfortGap: CGFloat = 24

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
        // Scroll-under-glass: the WKWebView transcript renders full-bleed so the
        // glass blur shows content scrolling under the floating control card.
        // SwiftUI already sums the entire bottom obstruction — the
        // .safeAreaInset chrome + the home indicator — into
        // safeAreaInsets.bottom. We read that single value and feed it to the DOM
        // padding, so there are no hand-reconstructed global frames or magic
        // offsets to drift out of sync when the keyboard, Dynamic Type, or chrome
        // height changes. The GeometryReader is the view .safeAreaInset is applied
        // to, so its proxy reports the *increased* bottom inset; the transcript
        // child then ignores that area only for drawing.
        GeometryReader { proxy in
            // Ignore BOTH .container and .keyboard so the WebView frame is fully
            // stable — it never shrinks when the keyboard opens. The control card
            // (a .safeAreaInset, below) still rises above the keyboard on its own.
            // Because the frame is stable, proxy.safeAreaInsets.bottom is the
            // single, correct clearance: keyboard + card when the keyboard is up,
            // card + home indicator when it's down. Feeding that to the
            // DOM padding avoids the double-count we'd get if the frame shrank for
            // the keyboard AND we padded for it too.
            transcript(bottomInset: proxy.safeAreaInsets.bottom + Self.transcriptComfortGap)
                .ignoresSafeArea([.container, .keyboard], edges: .bottom)
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            bottomChrome
                .frame(maxWidth: .infinity)
        }
        .navigationTitle(viewModel.detail?.displayTitle ?? fallbackTitle)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                loopModeToolbarButton
                watchButton
            }
        }
        .task(id: sessionId) { await viewModel.start(sessionId: sessionId, appState: appState) }
        .onDisappear {
            viewModel.pauseRealtime()
        }
        .onChange(of: scenePhase) { _, newPhase in
            // SSE over URLSession is foreground-only per Apple's contract.
            // Pause (not stop) on background/inactive so we drop the dead
            // connection but keep the session + transcript; restart on return
            // to active. SwiftUI can also fire onDisappear during app switch,
            // so that path must be non-destructive too.
            switch newPhase {
            case .active:
                Task { await viewModel.start(sessionId: sessionId, appState: appState) }
            case .background, .inactive:
                viewModel.pauseRealtime()
            @unknown default:
                break
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: UIApplication.didReceiveMemoryWarningNotification)) { _ in
            viewModel.handleMemoryWarning()
        }
        .onChange(of: viewModel.liveActivityFingerprint) { _, _ in
            guard let detail = viewModel.detail else { return }
            Task { await liveActivityManager.update(detail: detail) }
        }
        .refreshable { await viewModel.reload(sessionId: sessionId, appState: appState) }
    }

    // The fused floating control card: status line + composer (or the
    // unavailable row) in one translucent rounded surface, inset from the
    // bezel so the transcript scrolls under it. liveActivity (a Lock-Screen
    // failure, NOT runtime status) rides above as its own quiet pill.
    @ViewBuilder
    private var bottomChrome: some View {
        VStack(spacing: 8) {
            liveActivityMessage
            if viewModel.detail != nil {
                VStack(alignment: .leading, spacing: 8) {
                    runtimeDock
                    composer
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: 24, style: .continuous)
                        .fill(.ultraThinMaterial)
                        .overlay(
                            RoundedRectangle(cornerRadius: 24, style: .continuous)
                                .strokeBorder(.white.opacity(0.10), lineWidth: 0.75)
                        )
                )
                .shadow(color: .black.opacity(0.28), radius: 16, y: 5)
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("session-chat-bottom-chrome-card")
            }
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 10)
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
                    .labelStyle(.iconOnly)
                }
            }
            .disabled(liveActivityManager.isBusy)
            .accessibilityLabel(isWatching ? "Lock Screen updates on" : "Lock Screen updates")
            .accessibilityHint("Opens Lock Screen update options")
        }
    }

    @ViewBuilder
    private var loopModeToolbarButton: some View {
        if let detail = viewModel.detail {
            if viewModel.isUpdatingLoopMode {
                ProgressView()
                    .controlSize(.small)
                    .accessibilityLabel("Updating loop mode")
            } else {
                LoopModeButtons(
                    currentMode: detail.effectiveLoopMode,
                    disabled: false,
                    onChange: { mode in
                        Task { await viewModel.setLoopMode(sessionId: sessionId, mode: mode, appState: appState) }
                    }
                )
                .accessibilityIdentifier("session-loop-mode-controls")
            }
        }
    }

    @ViewBuilder
    private var runtimeDock: some View {
        if let detail = viewModel.detail {
            SessionRuntimeDock(detail: detail)
        }
    }

    // Lock-Screen / Live Activity management failure — explicitly NOT session
    // runtime status. A small attention pill above the control card.
    @ViewBuilder
    private var liveActivityMessage: some View {
        if let error = liveActivityManager.errorMessage {
            HStack(spacing: 6) {
                Image(systemName: "bell.slash")
                    .font(.caption2)
                Text(error)
                    .font(.caption)
                    .lineLimit(2)
                Spacer(minLength: 0)
            }
            .foregroundStyle(.orange)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(
                Capsule(style: .continuous).fill(.ultraThinMaterial)
            )
        }
    }

    private var transcriptState: TranscriptDisplayState {
        TranscriptDisplayState.derive(
            isInitialLoading: viewModel.isInitialLoading,
            hasContent: !viewModel.items.isEmpty || !viewModel.submittedInputs.isEmpty,
            errorMessage: viewModel.errorMessage,
            refreshErrorMessage: viewModel.refreshErrorMessage
        )
    }

    private func transcript(bottomInset: CGFloat) -> some View {
        let state = transcriptState
        let showTranscript = state.showsTranscript

        return ZStack {
            WebTranscriptView(
                serverURL: appState.serverURL,
                items: viewModel.items,
                submittedInputs: viewModel.submittedInputs,
                errorMessage: viewModel.errorMessage,
                bottomInset: bottomInset,
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

            TranscriptStateOverlay(
                state: state,
                onRetry: { Task { await viewModel.reload(sessionId: sessionId, appState: appState) } }
            )
        }
    }

    @ViewBuilder
    private var composer: some View {
        if let detail = viewModel.detail {
            if detail.activePauseRequest != nil || detail.canSendLive {
                composerField(detail: detail)
            } else {
                unavailableComposerFooter(detail: detail)
            }
        }
    }

    private func composerField(detail: SessionDetail) -> some View {
        let pauseRequest = detail.activePauseRequest
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

            if let pauseRequest {
                SessionPauseRequestCard(
                    pauseRequest: pauseRequest,
                    isResponding: viewModel.isRespondingToPauseRequest,
                    errorMessage: viewModel.pauseResponseErrorMessage,
                    onRespond: { decision, answers, content, message in
                        await viewModel.respondToPauseRequest(
                            sessionId: sessionId,
                            appState: appState,
                            pauseRequest: pauseRequest,
                            decision: decision,
                            answers: answers,
                            content: content,
                            message: message
                        )
                    }
                )
            } else if detail.shouldShowAttentionFallback {
                SessionAttentionFallbackCard(detail: detail)
            }

            if detail.attachImagesEnabled && pauseRequest == nil {
                attachmentTray
            }

            if pauseRequest == nil {
                HStack(alignment: .bottom, spacing: 8) {
                    composerActionMenu(detail: detail)

                    TextField(detail.composerPlaceholder, text: $composerText, axis: .vertical)
                        .lineLimit(1...6)
                        .focused($composerFocused)
                        .disabled(viewModel.isDrafting)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                        .accessibilityIdentifier("session-chat-composer")

                    // Send button: monochrome circle (light fill + dark glyph when
                    // armed, ghost when empty). Long-press reveals steer/queue split.
                    Button {
                        Task { await send() }
                    } label: {
                        if viewModel.isSending {
                            ProgressView()
                                .frame(width: 30, height: 30)
                        } else {
                            Image(systemName: sendIcon)
                                .font(.subheadline.weight(.bold))
                                .foregroundStyle(composerHasContent ? Color(.systemBackground) : Color(.systemGray))
                                .frame(width: 30, height: 30)
                                .background(
                                    Circle().fill(composerHasContent
                                        ? AnyShapeStyle(Color.primary)
                                        : AnyShapeStyle(Color(.tertiarySystemFill)))
                                )
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
        }
        .photosPicker(
            isPresented: $isShowingPhotoPicker,
            selection: $pickerSelection,
            maxSelectionCount: max(1, attachmentStore.slotsLeft),
            matching: .images
        )
        .onChange(of: pickerSelection) { _, items in
            guard !items.isEmpty else { return }
            let slotsLeftAtSelection = attachmentStore.slotsLeft
            guard slotsLeftAtSelection > 0 else {
                attachmentStore.errorMessage = "Max \(ComposerAttachmentLimits.maxAttachments) attachments."
                pickerSelection = []
                return
            }
            let itemsToLoad = Array(items.prefix(slotsLeftAtSelection))
            let skippedSelectionCount = items.count - itemsToLoad.count
            Task {
                await MainActor.run { isLoadingPickerItems = true }
                var raw: [(filename: String, data: Data)] = []
                var loadFailures = 0
                for _ in itemsToLoad.indices {
                    raw.append((filename: "", data: Data()))
                }
                for (idx, item) in itemsToLoad.enumerated() {
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
                    } else if skippedSelectionCount > 0 {
                        let slotNoun = slotsLeftAtSelection == 1 ? "slot" : "slots"
                        attachmentStore.errorMessage = "Only \(slotsLeftAtSelection) attachment \(slotNoun) left."
                    }
                    pickerSelection = []
                    isLoadingPickerItems = false
                }
            }
        }
    }

    private func composerActionMenu(detail: SessionDetail) -> some View {
        let attachmentSlotsLeft = attachmentStore.slotsLeft
        let attachmentIsProcessing = attachmentStore.isProcessing || isLoadingPickerItems
        let canAttachImages = attachmentInputEnabled
            && attachmentSlotsLeft > 0
            && !attachmentIsProcessing
            && !viewModel.isSending
        let canDraft = !composerHasText && !viewModel.isSending && !viewModel.isDrafting

        return Menu {
            Button {
                Task { await draft() }
            } label: {
                Label("Draft reply", systemImage: "sparkles")
            }
            .disabled(!canDraft)

            if detail.attachImagesEnabled {
                Button {
                    isShowingPhotoPicker = true
                } label: {
                    Label("Attach images", systemImage: "paperclip")
                }
                .disabled(!canAttachImages)
                .accessibilityIdentifier("session-chat-attach")
            }
        } label: {
            Group {
                if viewModel.isDrafting || attachmentIsProcessing {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: "plus")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
            }
            .frame(width: 32, height: 32)
            .contentShape(Rectangle())
        }
        .disabled(viewModel.isSending)
        .accessibilityLabel("Message actions")
        .accessibilityIdentifier("session-chat-compose-actions")
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

    // Degraded/observe-only/offline/ended: composer is replaced by an
    // explanatory row. Copy comes straight from the capability model — no
    // invented state strings (canSendLive remains the hard gate upstream).
    private func unavailableComposerFooter(detail: SessionDetail) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: detail.isControlOffline ? "wifi.slash" : "eye")
                .font(.body)
                .foregroundStyle(detail.isControlOffline ? .orange : .secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(detail.runtimeCapabilityLabel)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
                if let message = detail.controlHealthMessage {
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 4)
        .padding(.vertical, 4)
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

    // Bare glyphs — the surrounding circle is drawn by the send button itself.
    private var sendIcon: String {
        switch primaryIntent {
        case "queue": return "clock.arrow.circlepath"
        default: return "arrow.up"
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
            // Re-ingest compressed attachments after a terminal failure or
            // ambiguous confirmation so the user can decide whether to retry.
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

private struct SessionAttentionFallbackCard: View {
    let detail: SessionDetail

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider().opacity(0.4)

            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "exclamationmark.bubble")
                    .font(.subheadline)
                    .foregroundStyle(.orange)
                    .frame(width: 18, height: 18)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Needs attention")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.orange)
                    Text(detail.runtimeHeadline)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                    Text(detail.runtimeDetail ?? "Check the original terminal for the next step.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                }
                Spacer(minLength: 0)
            }

            Label("Waiting in terminal", systemImage: "terminal")
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("session-attention-fallback")
    }
}

// Per-page natural content height, keyed by question index, so the pause card
// can hug short questions and only scroll when one genuinely overflows.
private struct PauseQuestionHeightKey: PreferenceKey {
    static let defaultValue: [Int: CGFloat] = [:]
    static func reduce(value: inout [Int: CGFloat], nextValue: () -> [Int: CGFloat]) {
        value.merge(nextValue(), uniquingKeysWith: { max($0, $1) })
    }
}

private struct SessionPauseRequestCard: View {
    let pauseRequest: SessionPauseRequest
    let isResponding: Bool
    let errorMessage: String?
    let onRespond: (
        _ decision: String,
        _ answers: [String: [String]]?,
        _ content: String?,
        _ message: String?
    ) async -> Bool

    @State private var answers: [String: [String]]
    @State private var fallbackText: String
    @State private var submitted = false
    @State private var currentPage = 0
    @State private var measuredHeights: [Int: CGFloat] = [:]

    init(
        pauseRequest: SessionPauseRequest,
        isResponding: Bool,
        errorMessage: String?,
        onRespond: @escaping (
            _ decision: String,
            _ answers: [String: [String]]?,
            _ content: String?,
            _ message: String?
        ) async -> Bool
    ) {
        self.pauseRequest = pauseRequest
        self.isResponding = isResponding
        self.errorMessage = errorMessage
        self.onRespond = onRespond
        _answers = State(initialValue: Self.initialAnswers(for: pauseRequest.questions))
        _fallbackText = State(initialValue: "")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider().opacity(0.4)

            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "questionmark.bubble")
                    .font(.subheadline)
                    .foregroundStyle(.orange)
                    .frame(width: 18, height: 18)
                VStack(alignment: .leading, spacing: 2) {
                    Text(isPermissionPrompt ? "Permission" : "Needs answer")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.orange)
                    Text(pauseRequest.title?.nonEmptyTrimmed ?? (isPermissionPrompt ? "Tool permission" : "Provider question"))
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                    Text(detailText)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                }
                Spacer(minLength: 0)
                if pageCount > 1 {
                    pageDots
                }
            }

            if pauseRequest.questions.isEmpty && !isPermissionPrompt {
                if pauseRequest.canRespond {
                    TextField("Answer", text: $fallbackText, axis: .vertical)
                        .lineLimit(1...4)
                        .disabled(isDisabled)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 8)
                        .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                        .accessibilityIdentifier("session-pause-freeform")
                }
            } else if pageCount > 1 {
                // One question per page. Each page scrolls only when its content
                // is taller than the cap; otherwise the region hugs the content
                // so there's no dead space between the question and the footer.
                TabView(selection: $currentPage) {
                    ForEach(Array(pauseRequest.questions.enumerated()), id: \.offset) { index, question in
                        questionPage(question: question, index: index)
                            .tag(index)
                    }
                }
                .tabViewStyle(.page(indexDisplayMode: .never))
                .frame(height: resolvedPageHeight)
                .animation(.easeInOut(duration: 0.22), value: currentPage)
                .animation(.easeInOut(duration: 0.22), value: resolvedPageHeight)
                .accessibilityIdentifier("session-pause-pager")
            } else if let question = pauseRequest.questions.first {
                questionPage(question: question, index: 0)
                    .frame(height: resolvedPageHeight)
            }

            if let errorMessage {
                Text(errorMessage)
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .accessibilityIdentifier("session-pause-error")
            }

            footer
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("session-pause-card")
        .onChange(of: pauseRequest.id) { _, _ in
            answers = Self.initialAnswers(for: pauseRequest.questions)
            fallbackText = ""
            submitted = false
            currentPage = 0
            // Drop stale heights so a new, shorter request doesn't briefly
            // inherit the previous card's height (reintroducing dead space).
            measuredHeights = [:]
        }
    }

    private var pageCount: Int { pauseRequest.questions.count }

    private var isLastPage: Bool { currentPage >= pageCount - 1 }

    // Upper bound so the pinned footer stays on screen regardless of how many
    // long-description options a single question carries. Below this the region
    // hugs the measured content height instead of reserving the full cap.
    private var pageMaxHeight: CGFloat { 340 }

    // Size the question region to the current page's actual content, clamped to
    // the cap. Falls back to the cap until the page reports its height so the
    // footer never jumps off-screen on first layout.
    private var resolvedPageHeight: CGFloat {
        guard let measured = measuredHeights[currentPage] else { return pageMaxHeight }
        return min(measured, pageMaxHeight)
    }

    // A single question, scrollable only when it overflows the cap, reporting
    // its natural content height back up for the hug-to-content sizing.
    @ViewBuilder
    private func questionPage(question: SessionPauseQuestion, index: Int) -> some View {
        ScrollView {
            questionView(question: question, index: index)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    GeometryReader { proxy in
                        Color.clear.preference(
                            key: PauseQuestionHeightKey.self,
                            value: [index: proxy.size.height]
                        )
                    }
                )
        }
        .scrollBounceBehavior(.basedOnSize)
        .onPreferenceChange(PauseQuestionHeightKey.self) { heights in
            for (key, value) in heights {
                measuredHeights[key] = value
            }
        }
    }

    private var pageDots: some View {
        HStack(spacing: 5) {
            ForEach(0..<pageCount, id: \.self) { index in
                Circle()
                    .fill(index == currentPage ? Color.accentColor : Color.secondary.opacity(0.4))
                    .frame(width: 6, height: 6)
            }
        }
        .accessibilityLabel("Question \(currentPage + 1) of \(pageCount)")
    }

    @ViewBuilder
    private var footer: some View {
        HStack(spacing: 8) {
            if pauseRequest.canRespond {
                if pageCount > 1 && currentPage > 0 {
                    Button {
                        withAnimation { currentPage -= 1 }
                    } label: {
                        Label("Back", systemImage: "chevron.left")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .disabled(isDisabled)
                    .accessibilityIdentifier("session-pause-back")
                }

                if pageCount > 1 && !isLastPage {
                    Button {
                        withAnimation { currentPage += 1 }
                    } label: {
                        Label("Next", systemImage: "chevron.right")
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                    .disabled(isDisabled || !currentPageAnswered)
                    .accessibilityHint(currentPageAnswered ? "" : "Select an option to continue")
                    .accessibilityIdentifier("session-pause-next")
                } else {
                    Button {
                        Task { await submitAnswer() }
                    } label: {
                        if isResponding {
                            ProgressView().controlSize(.mini)
                        } else {
                            Label(primaryActionLabel, systemImage: "checkmark.circle")
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                    .disabled(!canSubmitAnswer || isDisabled)
                    .accessibilityIdentifier("session-pause-send")
                }

                Button {
                    Task { await cancelRequest() }
                } label: {
                    Label(secondaryActionLabel, systemImage: "xmark.circle")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(isDisabled)
                .accessibilityIdentifier("session-pause-cancel")
            } else {
                Label("Waiting in terminal", systemImage: "terminal")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
    }

    private var currentPageAnswered: Bool {
        guard pauseRequest.questions.indices.contains(currentPage) else { return true }
        let question = pauseRequest.questions[currentPage]
        let key = Self.questionKey(question, index: currentPage)
        return answers[key, default: []].contains { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }

    @ViewBuilder
    private func questionView(question: SessionPauseQuestion, index: Int) -> some View {
        let key = Self.questionKey(question, index: index)
        VStack(alignment: .leading, spacing: 5) {
            if let header = question.header?.nonEmptyTrimmed {
                Text(header)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            Text(question.question)
                .font(.caption)
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)

            if isPlanApproval {
                EmptyView()
            } else if question.options.isEmpty {
                if pauseRequest.canRespond {
                    TextField("Answer", text: Binding(
                        get: { answers[key]?.first ?? "" },
                        set: { answers[key] = [$0] }
                    ), axis: .vertical)
                    .lineLimit(1...3)
                    .disabled(isDisabled)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                    .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                }
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(question.options.enumerated()), id: \.offset) { optionIndex, option in
                        optionButton(question: question, option: option, key: key, optionIndex: optionIndex)
                    }
                }
            }
        }
    }

    private func optionButton(
        question: SessionPauseQuestion,
        option: SessionPauseQuestionOption,
        key: String,
        optionIndex: Int
    ) -> some View {
        let value = Self.optionValue(option)
        let selected = answers[key, default: []].contains(value)
        return Button {
            if question.multiSelect {
                toggleValue(value, for: key)
            } else {
                answers[key] = [value]
            }
        } label: {
            HStack(alignment: .top, spacing: 7) {
                Image(systemName: selected ? "checkmark.circle.fill" : (question.multiSelect ? "square" : "circle"))
                    .font(.caption)
                    .foregroundStyle(selected ? Color.accentColor : Color.secondary)
                    .frame(width: 16, height: 16)
                VStack(alignment: .leading, spacing: 1) {
                    Text(option.label)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                    if let description = option.description?.nonEmptyTrimmed {
                        Text(description)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(isDisabled)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("session-pause-option-\(key)-\(optionIndex)")
    }

    private var providerLabel: String {
        pauseRequest.provider.prefix(1).uppercased() + String(pauseRequest.provider.dropFirst())
    }

    private var detailText: String {
        if let summary = pauseRequest.summary?.nonEmptyTrimmed {
            return summary
        }
        return pauseRequest.canRespond
            ? "\(providerLabel) is waiting for your answer."
            : "Answer this in the terminal or reconnect the host."
    }

    private var isDisabled: Bool {
        isResponding || submitted || !pauseRequest.canRespond
    }

    // Permission prompts (tool allow/deny) reuse this card but read as Allow/Deny
    // and need no answer text.
    private var isPermissionPrompt: Bool {
        pauseRequest.kind == "permission_prompt"
    }

    private var isPlanApproval: Bool {
        pauseRequest.kind == "plan_approval"
    }

    private var primaryActionLabel: String {
        if isPermissionPrompt { return "Allow" }
        if isPlanApproval { return "Approve" }
        return "Send answer"
    }

    private var secondaryActionLabel: String {
        if isPermissionPrompt { return "Deny" }
        if isPlanApproval { return "Reject" }
        return "Cancel"
    }

    private var canSubmitAnswer: Bool {
        if isPermissionPrompt || isPlanApproval {
            return true
        }
        if pauseRequest.questions.isEmpty {
            return !fallbackText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
        return pauseRequest.questions.enumerated().allSatisfy { index, question in
            let key = Self.questionKey(question, index: index)
            return answers[key, default: []].contains { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        }
    }

    private func submitAnswer() async {
        let structuredAnswers: [String: [String]]?
        let content: String?
        if pauseRequest.questions.isEmpty {
            structuredAnswers = nil
            content = fallbackText.trimmingCharacters(in: .whitespacesAndNewlines)
        } else {
            structuredAnswers = normalizedAnswers()
            content = nil
        }
        let ok = await onRespond(
            "answer",
            structuredAnswers,
            content,
            answerMessage(structuredAnswers: structuredAnswers, content: content)
        )
        if ok { submitted = true }
    }

    private func cancelRequest() async {
        let ok = await onRespond("cancel", nil, nil, "Cancelled in Longhouse.")
        if ok { submitted = true }
    }

    private func normalizedAnswers() -> [String: [String]] {
        Dictionary(uniqueKeysWithValues: pauseRequest.questions.enumerated().map { index, question in
            let key = Self.questionKey(question, index: index)
            let values = answers[key, default: []]
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
            return (key, values)
        })
    }

    private func answerMessage(structuredAnswers: [String: [String]]?, content: String?) -> String? {
        if let structuredAnswers {
            let parts = pauseRequest.questions.enumerated().compactMap { index, question -> String? in
                let key = Self.questionKey(question, index: index)
                guard let values = structuredAnswers[key], !values.isEmpty else { return nil }
                let label = question.header?.nonEmptyTrimmed ?? question.question
                return "\(label): \(values.joined(separator: ", "))"
            }
            return parts.isEmpty ? nil : parts.joined(separator: "; ")
        }
        return content?.nonEmptyTrimmed
    }

    private func toggleValue(_ value: String, for key: String) {
        var values = answers[key, default: []]
        if values.contains(value) {
            values.removeAll { $0 == value }
        } else {
            values.append(value)
        }
        answers[key] = values
    }

    private static func initialAnswers(for questions: [SessionPauseQuestion]) -> [String: [String]] {
        Dictionary(uniqueKeysWithValues: questions.enumerated().map { index, question in
            (questionKey(question, index: index), [])
        })
    }

    private static func questionKey(_ question: SessionPauseQuestion, index: Int) -> String {
        let trimmed = question.id.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "question-\(index + 1)" : trimmed
    }

    private static func optionValue(_ option: SessionPauseQuestionOption) -> String {
        let raw = option.value ?? option.label
        return raw.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

private extension String {
    var nonEmptyTrimmed: String? {
        let trimmed = trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}

// Mirrors `bottomChrome`: the card rides in a translucent rounded surface
// pinned to the bottom with transcript space above, so previews read the way
// the screen actually looks instead of floating in black.
private struct PauseRequestPreviewChrome<Content: View>: View {
    @ViewBuilder var content: Content

    var body: some View {
        VStack(spacing: 0) {
            Spacer(minLength: 0)
            VStack(alignment: .leading, spacing: 8) {
                content
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: 24, style: .continuous)
                    .fill(.ultraThinMaterial)
                    .overlay(
                        RoundedRectangle(cornerRadius: 24, style: .continuous)
                            .strokeBorder(.white.opacity(0.10), lineWidth: 0.75)
                    )
            )
            .shadow(color: .black.opacity(0.28), radius: 16, y: 5)
            .padding(.horizontal, 12)
            .padding(.bottom, 10)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(.systemBackground))
        .preferredColorScheme(.dark)
    }
}

#Preview("Session pause request") {
    PauseRequestPreviewChrome {
        SessionPauseRequestCard(
            pauseRequest: SessionPauseRequest(
                id: "pause-preview",
                sessionId: "session-preview",
                runtimeKey: "codex:session-preview",
                kind: "structured_question",
                status: "pending",
                provider: "codex",
                canRespond: true,
                title: "Choose storage",
                summary: "Codex needs a storage decision before it can continue.",
                toolName: "requestUserInput",
                questions: [
                    SessionPauseQuestion(
                        id: "storage",
                        header: "Storage",
                        question: "Which storage backend should I implement?",
                        multiSelect: false,
                        options: [
                            SessionPauseQuestionOption(label: "SQLite", description: "Keep it local and simple.", value: "sqlite"),
                            SessionPauseQuestionOption(label: "Postgres", description: "Use managed database features.", value: "postgres"),
                        ]
                    )
                ],
                occurredAt: nil,
                lastSeenAt: nil,
                resolvedAt: nil,
                expiresAt: nil
            ),
            isResponding: false,
            errorMessage: nil,
            onRespond: { _, _, _, _ in true }
        )
    }
}

#Preview("Session pause request · multi-question pager") {
    PauseRequestPreviewChrome {
        SessionPauseRequestCard(
            pauseRequest: SessionPauseRequest(
                id: "pause-multi-preview",
                sessionId: "session-preview",
                runtimeKey: "claude:session-preview",
                kind: "structured_question",
                status: "pending",
                provider: "claude",
                canRespond: true,
                title: "Wait model",
                summary: "Waiting for your answer.",
                toolName: "AskUserQuestion",
                questions: [
                    SessionPauseQuestion(
                        id: "wait_model",
                        header: "Wait model",
                        question: "Your phone's in your pocket — replies can lag 30-60 min, well past a sane block window. How should the agent behave when it asks for approval?",
                        multiSelect: false,
                        options: [
                            SessionPauseQuestionOption(label: "Async grant: request, don't block, resume on reply", description: "Agent sends the SMS and returns 'pending' immediately (no long block). Your YES — whenever it lands — creates a standing time-boxed grant. The agent checks back and proceeds the moment the grant exists.", value: "async"),
                            SessionPauseQuestionOption(label: "Long block with grace, then convert to async", description: "Block for a modest window (e.g. 10 min) for the common quick-reply case; if it times out, DON'T discard — leave the request standing so a later reply still grants access.", value: "grace"),
                            SessionPauseQuestionOption(label: "Approve-ahead / batch", description: "Agent lists everything it'll need up front, sends ONE approval, you reply once, and it proceeds through all of them. Fewer texts, front-loads the wait.", value: "batch"),
                        ]
                    ),
                    SessionPauseQuestion(
                        id: "late_reply",
                        header: "Late reply",
                        question: "When a YES finally lands after the agent moved on, what should happen?",
                        multiSelect: false,
                        options: [
                            SessionPauseQuestionOption(label: "Stand as a grant for a window", description: "A late YES creates a time-boxed grant (e.g. valid 1h). Next time anything needs that cred within the window, it proceeds with NO new SMS. (Recommended)", value: "grant"),
                            SessionPauseQuestionOption(label: "Notify + resume the paused task", description: "A late YES actively pings the waiting agent/task to wake up and continue right then. More 'live' but needs a running listener + task-resume plumbing.", value: "resume"),
                            SessionPauseQuestionOption(label: "Just record it, require fresh request", description: "Late YES is logged but does nothing on its own; the agent must re-request next time. Simplest, but wastes your reply.", value: "record"),
                        ]
                    ),
                ],
                occurredAt: nil,
                lastSeenAt: nil,
                resolvedAt: nil,
                expiresAt: nil
            ),
            isResponding: false,
            errorMessage: nil,
            onRespond: { _, _, _, _ in true }
        )
    }
}

#Preview("Session pause request · terminal only") {
    PauseRequestPreviewChrome {
        SessionPauseRequestCard(
            pauseRequest: SessionPauseRequest(
                id: "pause-terminal-preview",
                sessionId: "session-preview",
                runtimeKey: "claude:session-preview",
                kind: "structured_question",
                status: "pending",
                provider: "claude",
                canRespond: false,
                title: "Claude needs an answer",
                summary: "Answer this in the original terminal.",
                toolName: "AskUserQuestion",
                questions: [
                    SessionPauseQuestion(
                        id: "terminal_answer",
                        header: nil,
                        question: "Claude is waiting for an interactive answer in the terminal.",
                        multiSelect: false,
                        options: []
                    )
                ],
                occurredAt: nil,
                lastSeenAt: nil,
                resolvedAt: nil,
                expiresAt: nil
            ),
            isResponding: false,
            errorMessage: nil,
            onRespond: { _, _, _, _ in true }
        )
    }
}

struct SessionRuntimeDock: View {
    let detail: SessionDetail

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        // One quiet monochrome status line. The state dot is the only color;
        // headline/detail/capability are a flat type hierarchy. No background or
        // divider — the fused control card owns the surface.
        HStack(spacing: 7) {
            indicator
            Text(detail.runtimeHeadline)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.primary)
                .lineLimit(1)
            if let runtimeDetail = detail.runtimeDetail, !typeSize.isAccessibilitySize {
                Text("·").foregroundStyle(.tertiary)
                Text(runtimeDetail)
                    .font(.subheadline)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            capabilityPill
        }
        .padding(.horizontal, 4)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(accessibilityLabel)
    }

    private var style: RuntimeChromeStyle { RuntimeChromeStyle(detail: detail) }

    // State dot — color is the signal; motion (breathing ring) marks "live".
    @ViewBuilder
    private var indicator: some View {
        let toneColor = style.dot.color
        ZStack {
            if detail.isSessionExecuting && !reduceMotion {
                Circle().stroke(toneColor.opacity(0.35), lineWidth: 1.5)
                    .frame(width: 13, height: 13)
            }
            Circle().fill(toneColor).frame(width: 7, height: 7)
        }
        .frame(width: 14, height: 14)
    }

    // Capability as monochrome text with a small live-dot — never colored words.
    private var capabilityPill: some View {
        HStack(spacing: 4) {
            if style.capability.showsLiveDot {
                Circle().fill(TranscriptPalette.live).frame(width: 5, height: 5)
            }
            Text(capabilityLabel)
                .font(.caption2.weight(.medium))
                .lineLimit(1)
        }
        .foregroundStyle(style.capability.color)
    }

    private var capabilityLabel: String {
        let label = detail.runtimeCapabilityLabel
        let livePrefix = "Live on "
        if label.range(of: livePrefix, options: [.anchored, .caseInsensitive]) != nil {
            let hostStart = label.index(label.startIndex, offsetBy: livePrefix.count)
            let host = label[hostStart...].trimmingCharacters(in: .whitespacesAndNewlines)
            if !host.isEmpty {
                return host
            }
        }
        return label
    }

    private var accessibilityLabel: String {
        [detail.runtimeHeadline, detail.runtimeDetail, capabilityLabel]
            .compactMap { $0 }
            .joined(separator: ", ")
    }
}


struct LoopModeButtons: View {
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
            HStack(spacing: 3) {
                Image(systemName: modeIcon)
                    .font(.caption2.weight(.semibold))
                if !typeSize.isAccessibilitySize {
                    Text(modeLabel)
                        .font(.caption2.weight(.medium))
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                }
            }
            .lineLimit(1)
            .fixedSize(horizontal: true, vertical: false)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(Color(.quaternarySystemFill), in: Capsule())
        }
        .disabled(disabled)
        .accessibilityLabel("Loop mode: \(modeLabel)")
    }

    @Environment(\.dynamicTypeSize) private var typeSize

    private var modeLabel: String {
        switch currentMode {
        case .assist: return "Assist"
        case .autopilot: return "Auto"
        case .manual: return "Off"
        }
    }

    private var modeIcon: String {
        switch currentMode {
        case .assist: return "wand.and.stars"
        case .autopilot: return "bolt"
        case .manual: return "pause"
        }
    }
}

enum SubmittedInputPhase: String, Sendable {
    case submitting
    case sent
    case queued
    case couldNotConfirm
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
    /// Blocking load error: only set when there is genuinely nothing to show
    /// (no cache, never loaded). Drives the full-screen error overlay.
    @Published var errorMessage: String?
    /// Non-blocking refresh failure: set when a reconnect/refresh fails but we
    /// already have cached content on screen. Drives a thin banner over the
    /// transcript instead of erasing it.
    @Published var refreshErrorMessage: String?
    @Published var isInitialLoading = true
    @Published var isSending = false
    @Published var isDrafting = false
    @Published var isUpdatingLoopMode = false
    @Published var isRespondingToPauseRequest = false
    @Published var draftErrorMessage: String?
    @Published var loopModeErrorMessage: String?
    @Published var pauseResponseErrorMessage: String?
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
    private var realtimeRefreshRetryTask: Task<Void, Never>?
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
    private var loadedProjectionItemCount = 0
    private var totalProjectionItemCount = 0
    private var tailSnapshotEventId: Int?
    private var prefetchedOlderTail: SessionMobileTailResponse?
    private var prefetchedOlderOffset: Int?
    private var prefetchedOlderSnapshotEventId: Int?
    private var prefetchInFlightOffset: Int?
    private var prefetchInFlightSnapshotEventId: Int?
    private var prefetchInFlightToken: Int?
    private var nextPrefetchToken = 0
    private var isLoadingOlder = false
    private var openWaterfall: SessionOpenWaterfall?
    private let apiFactory: (String) -> SessionWorkspaceClient?
    private let streamFactory: (URL, String, Int?, String?) -> SessionWorkspaceStreamSource
    private let enableRealtime: Bool
    private let transcriptCache: SessionTranscriptCache?
    /// Durable on-disk mirror of the tail; survives app eviction so a cold
    /// relaunch can hydrate before the network responds. The in-memory
    /// `transcriptCache` stays as the fast warm-resume path.
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
        transcriptCache: SessionTranscriptCache? = nil,
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
        self.transcriptCache = transcriptCache ?? (enableRealtime ? .shared : nil)
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
            submittedInputs = []
            transcriptDiagnostics = nil
            pendingRealtimeTelemetry = nil
            lastWorkspaceEvents = []
            loadedProjectionItemCount = 0
            totalProjectionItemCount = 0
            tailSnapshotEventId = nil
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
            realtimeRefreshFailureCount = 0
            errorMessage = nil
            refreshErrorMessage = nil
            pauseResponseErrorMessage = nil
            lastPubsubSeq = nil
            lastWorkspaceRevisionFingerprint = nil
            streamAuthRefreshAttempted = false
            // Warm path: in-memory cache survives backgrounding while the
            // process lives. Cold path: the durable on-disk snapshot survives
            // app eviction, so a relaunch into a session renders instantly
            // instead of a blank screen + lone warning triangle.
            if let snapshot = transcriptCache?.snapshot(serverURL: appState.serverURL, sessionId: sessionId) {
                let ageMs = Int(Date().timeIntervalSince(snapshot.savedAt) * 1000)
                openWaterfall?.mark(
                    "cache_hit",
                    "events=\(snapshot.events.count) age_ms=\(ageMs)"
                )
                applyCachedSnapshot(snapshot)
                restoredFromCache = true
                // Cache is an instant paint, not the source of truth. Notification
                // opens often land seconds after new transcript rows, while the
                // SSE stream uses skip_initial=true and can miss the event that
                // caused the notification. Always reconcile after restoring.
                shouldRefreshCachedTail = true
            } else if let disk = snapshotStore?.load(serverURL: appState.serverURL, sessionId: sessionId) {
                let ageMs = Int(Date().timeIntervalSince(disk.savedAt) * 1000)
                openWaterfall?.mark(
                    "disk_hit",
                    "events=\(disk.events.count) age_ms=\(ageMs)"
                )
                applyDiskSnapshot(disk)
                restoredFromCache = true
                // Disk snapshots are typically older than the in-memory grace
                // window; always reconcile in the background after rendering.
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
        do {
            try await refreshTail(api: api, sessionId: sessionId)
            errorMessage = nil
            refreshErrorMessage = nil
            loopModeErrorMessage = nil
        } catch LonghouseAPIError.notAuthenticated {
            if hasContent {
                refreshErrorMessage = "Session expired. Pull to refresh."
            } else {
                errorMessage = "Session expired."
            }
        } catch {
            if hasContent {
                refreshErrorMessage = "Couldn't refresh. Showing saved messages."
            } else {
                errorMessage = "Couldn't load session. Pull to refresh."
            }
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
            var ticks = 0
            while !Task.isCancelled {
                guard let self = self else { break }
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                if Task.isCancelled { break }
                ticks += 1
                let (connected, hasRunningTool) = await MainActor.run {
                    (
                        self.streamConnected,
                        self.lastWorkspaceEvents.contains { $0.toolCallState == .running }
                    )
                }
                let managed = await MainActor.run {
                    guard let caps = self.detail?.capabilities else { return false }
                    return caps.liveControlAvailable == true || caps.hostReattachAvailable == true
                }
                // Fast fallback when SSE is down. When SSE is up, the server
                // flips unpaired tool calls to "dropped" lazily on read, so
                // re-ask every ~60s while a running tool exists to keep
                // tool_call_state honest if the stream stays quiet. Managed
                // visible sessions also get a low-rate correctness poll because
                // SSE is an invalidation path, not the transcript source of truth.
                if Self.shouldPollVisibleSession(
                    connected: connected,
                    hasRunningTool: hasRunningTool,
                    managed: managed,
                    ticks: ticks
                ) {
                    await self.pollTick(sessionId: sessionId, appState: appState)
                }
            }
        }
    }

    static func shouldPollVisibleSession(
        connected: Bool,
        hasRunningTool: Bool,
        managed: Bool,
        ticks: Int
    ) -> Bool {
        if !connected { return true }
        if hasRunningTool, ticks % 12 == 0 { return true }
        if managed, ticks % 3 == 0 { return true }
        return false
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
            if let seq = change.pubsub_seq {
                lastPubsubSeq = seq
            }
            openWaterfall?.mark(
                "stream_changed",
                "latest=\(change.latest_event_id) seq=\(change.pubsub_seq ?? 0) preview=\(change.transcript_preview != nil)"
            )
            if let transcriptPreview = change.transcript_preview?.sessionTranscriptPreview {
                applyRealtimeTranscriptPreview(transcriptPreview, sessionId: sessionId)
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
            let refreshedLoadedCount = min(
                tail.projection.total,
                max(0, max(tail.projection.total - tail.projection.pageOffset, mergedEvents.count))
            )
            self.loadedProjectionItemCount = refreshedLoadedCount
            let keepPrefetchedOlderTail = prefetchedOlderOffset == refreshedLoadedCount
                && prefetchedOlderSnapshotEventId == tail.snapshotEventId
            self.totalProjectionItemCount = tail.projection.total
            self.tailSnapshotEventId = tail.snapshotEventId
            self.lastWorkspaceRevisionFingerprint = tail.workspaceRevision?.fingerprint
            if !keepPrefetchedOlderTail {
                self.prefetchedOlderTail = nil
                self.prefetchedOlderOffset = nil
                self.prefetchedOlderSnapshotEventId = nil
            }
            let builtItems = TimelineBuilder.build(
                events: TranscriptPreviewProjection.visibleEvents(
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
            snapshotEventId: tailSnapshotEventId
        )
    }

    private func applyOlderTail(_ tail: SessionMobileTailResponse) {
        totalProjectionItemCount = tail.projection.total
        lastWorkspaceRevisionFingerprint = tail.workspaceRevision?.fingerprint ?? lastWorkspaceRevisionFingerprint
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
        lastPubsubSeq = snapshot.lastPubsubSeq
        lastWorkspaceRevisionFingerprint = snapshot.workspaceRevisionFingerprint
        prefetchedOlderTail = nil
        prefetchedOlderOffset = nil
        prefetchedOlderSnapshotEventId = nil
        prefetchInFlightOffset = nil
        prefetchInFlightSnapshotEventId = nil
        prefetchInFlightToken = nil
        items = TimelineBuilder.build(events: snapshot.events)
        isInitialLoading = false
        errorMessage = nil
        refreshErrorMessage = nil
        openWaterfall?.mark("cache_applied", "events=\(snapshot.events.count) items=\(items.count)")
    }

    private func applyDiskSnapshot(_ snapshot: TranscriptSnapshotStore.Snapshot) {
        detail = snapshot.detail
        lastWorkspaceEvents = snapshot.events
        loadedProjectionItemCount = snapshot.loadedProjectionItemCount
        totalProjectionItemCount = snapshot.totalProjectionItemCount
        tailSnapshotEventId = snapshot.tailSnapshotEventId
        lastPubsubSeq = snapshot.lastPubsubSeq
        lastWorkspaceRevisionFingerprint = snapshot.workspaceRevisionFingerprint
        prefetchedOlderTail = nil
        prefetchedOlderOffset = nil
        prefetchedOlderSnapshotEventId = nil
        prefetchInFlightOffset = nil
        prefetchInFlightSnapshotEventId = nil
        prefetchInFlightToken = nil
        items = TimelineBuilder.build(events: snapshot.events)
        isInitialLoading = false
        errorMessage = nil
        refreshErrorMessage = nil
        openWaterfall?.mark("disk_applied", "events=\(snapshot.events.count) items=\(items.count)")
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
            refreshErrorMessage = "Couldn't refresh. Showing saved messages."
        }
        if activeSessionId == sessionId, let api = apiFactory(activeServerURL ?? "") {
            scheduleOlderPrefetch(api: api, sessionId: sessionId)
        }
    }

    private func saveCurrentCache() {
        guard let activeServerURL, let activeSessionId, let detail else { return }
        let storedDetail = detail.withoutTranscriptPreview
        transcriptCache?.store(
            serverURL: activeServerURL,
            sessionId: activeSessionId,
            detail: storedDetail,
            events: lastWorkspaceEvents,
            loadedProjectionItemCount: loadedProjectionItemCount,
            totalProjectionItemCount: totalProjectionItemCount,
            tailSnapshotEventId: tailSnapshotEventId,
            lastPubsubSeq: lastPubsubSeq,
            workspaceRevisionFingerprint: lastWorkspaceRevisionFingerprint
        )
        snapshotStore?.save(
            serverURL: activeServerURL,
            sessionId: activeSessionId,
            detail: storedDetail,
            events: lastWorkspaceEvents,
            loadedProjectionItemCount: loadedProjectionItemCount,
            totalProjectionItemCount: totalProjectionItemCount,
            tailSnapshotEventId: tailSnapshotEventId,
            lastPubsubSeq: lastPubsubSeq,
            workspaceRevisionFingerprint: lastWorkspaceRevisionFingerprint
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
                && (input.phase == .failed || input.phase == .couldNotConfirm || input.phase == .needsUserDecision)
        }
    }

    private func reconcileSubmittedInputs(with events: [SessionEvent]) {
        guard !submittedInputs.isEmpty else { return }
        submittedInputs.removeAll { input in
            guard input.phase == .sent
                || input.phase == .queued
                || input.phase == .submitting
                || input.phase == .couldNotConfirm
                || input.phase == .failed
            else { return false }
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
        let rd = detail.runtimeDisplay
        return [
            detail.id,
            detail.displayTitle,
            rd.state ?? "",
            rd.tone,
            rd.headline,
            rd.phaseLabel,
            rd.compactToolLabel ?? "",
            rd.lifecycle,
            rd.controlPath,
            rd.activityRecency,
            rd.hostState,
            String(rd.isLive),
            String(rd.isExecuting),
            String(rd.needsAttention),
            String(rd.isStalled),
            rd.pauseRequest?.id ?? "",
            rd.pauseRequest?.status ?? "",
            rd.pauseRequest?.title ?? "",
            detail.project ?? "",
            detail.provider,
        ].joined(separator: "|")
    }

    var isSessionEnded: Bool {
        guard let detail else { return false }
        return detail.isClosed
    }
}
