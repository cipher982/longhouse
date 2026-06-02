import SwiftUI
import PhotosUI
import UIKit

struct SessionWorkspaceStreamSource: Sendable {
    let start: @Sendable () async -> AsyncStream<SessionWorkspaceStream.Event>
    let stop: @Sendable () async -> Void
    let clockSkewMs: @Sendable () async -> Int64

    static func live(baseURL: URL, sessionId: String, sinceSeq: Int? = nil) -> SessionWorkspaceStreamSource {
        let stream = SessionWorkspaceStream(baseURL: baseURL, sessionId: sessionId, sinceSeq: sinceSeq)
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
    /// DOM clearance (pt) needed for the last transcript row to rest above the
    /// floating controls while the WebKit transcript scrolls full-bleed behind
    /// them.
    @State private var transcriptBottomInset: CGFloat = Self.minimumTranscriptBottomInset
    @State private var transcriptViewportFrame: CGRect = .null
    @State private var bottomChromeSurfaceFrame: CGRect = .null
    @State private var bottomChromeCardFrame: CGRect = .null
    @State private var keyboardPresented: Bool = false
    private static let minimumTranscriptBottomInset = SessionBottomInsetCalculator.minimumTranscriptBottomInset

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
        // Scroll-under-glass: the transcript and floating control card are
        // measured in the same global coordinate space. The DOM bottom padding
        // is exactly the overlap between the transcript viewport and the card's
        // visual top edge, so keyboard movement cannot introduce a second
        // inferred safe-area offset.
        transcript
            .ignoresSafeArea(.container, edges: .bottom)
            .background(
                GeometryReader { proxy in
                    Color.clear.preference(
                        key: TranscriptViewportFrameKey.self,
                        value: proxy.frame(in: .global)
                    )
                }
            )
            .safeAreaInset(edge: .bottom, spacing: 0) {
                bottomChrome
                    .background(
                        GeometryReader { proxy in
                            Color.clear.preference(
                                key: BottomChromeSurfaceFrameKey.self,
                                value: proxy.frame(in: .global)
                            )
                        }
                    )
                    .frame(maxWidth: .infinity)
            }
            .onPreferenceChange(TranscriptViewportFrameKey.self) { frame in
                transcriptViewportFrame = frame
                recalculateTranscriptBottomInset()
            }
            .onPreferenceChange(BottomChromeSurfaceFrameKey.self) { frame in
                bottomChromeSurfaceFrame = frame
                recalculateTranscriptBottomInset()
            }
            .onPreferenceChange(BottomChromeCardFrameKey.self) { frame in
                bottomChromeCardFrame = frame
                recalculateTranscriptBottomInset()
            }
            .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in
                keyboardPresented = true
                recalculateTranscriptBottomInset()
            }
            .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillChangeFrameNotification)) { _ in
                keyboardPresented = true
                recalculateTranscriptBottomInset()
            }
            .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardDidHideNotification)) { _ in
                keyboardPresented = false
                recalculateTranscriptBottomInset()
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
            // Pause (not stop) on background/inactive so we drop the dead
            // connection but keep the session + transcript; restart on return
            // to active. stop() is reserved for nav-away (.onDisappear) so an
            // unlock resumes the same session instead of erasing it.
            switch newPhase {
            case .active:
                Task { await viewModel.start(sessionId: sessionId, appState: appState) }
            case .background, .inactive:
                viewModel.pauseRealtime()
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
                .background(
                    GeometryReader { proxy in
                        Color.clear.preference(
                            key: BottomChromeCardFrameKey.self,
                            value: proxy.frame(in: .global)
                        )
                    }
                )
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("session-chat-bottom-chrome-card")
            }
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 10)
    }

    private func recalculateTranscriptBottomInset() {
        guard let inset = SessionBottomInsetCalculator.bottomInset(
            viewportFrame: transcriptViewportFrame,
            surfaceFrame: bottomChromeSurfaceFrame,
            cardFrame: bottomChromeCardFrame,
            keyboardPresented: keyboardPresented,
            screenMaxY: UIScreen.main.bounds.maxY
        ) else {
            return
        }
        transcriptBottomInset = inset
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

    private var transcript: some View {
        let state = transcriptState
        let showTranscript = state.showsTranscript

        return ZStack {
            WebTranscriptView(
                items: viewModel.items,
                submittedInputs: viewModel.submittedInputs,
                errorMessage: viewModel.errorMessage,
                bottomInset: transcriptBottomInset,
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

struct SessionBottomInsetCalculator {
    static let minimumTranscriptBottomInset: CGFloat = 18
    private static let bottomChromeShadowRadius: CGFloat = 16
    private static let bottomChromeShadowYOffset: CGFloat = 5
    private static let bottomChromeVisualBleedAbove: CGFloat = max(0, bottomChromeShadowRadius - bottomChromeShadowYOffset)
    private static let transcriptBottomComfortGap: CGFloat = 16

    static func bottomInset(
        viewportFrame: CGRect,
        surfaceFrame: CGRect,
        cardFrame: CGRect,
        keyboardPresented: Bool,
        screenMaxY: CGFloat
    ) -> CGFloat? {
        guard !viewportFrame.isNull, !surfaceFrame.isNull, !cardFrame.isNull else { return nil }

        let visualObstructionTop = min(cardFrame.minY, surfaceFrame.minY) - bottomChromeVisualBleedAbove
        let measuredViewportBottom = keyboardPresented
            ? viewportFrame.maxY
            : max(viewportFrame.maxY, screenMaxY)
        let measuredViewportOverlap = max(0, measuredViewportBottom - visualObstructionTop)
        let measuredChromeFloor = surfaceFrame.height + bottomChromeVisualBleedAbove
        let obstruction = max(measuredViewportOverlap, measuredChromeFloor)

        return max(
            minimumTranscriptBottomInset,
            obstruction + transcriptBottomComfortGap
        )
    }
}

private struct TranscriptViewportFrameKey: PreferenceKey {
    static let defaultValue = CGRect.null
    static func reduce(value: inout CGRect, nextValue: () -> CGRect) {
        let next = nextValue()
        if !next.isNull {
            value = next
        }
    }
}

private struct BottomChromeSurfaceFrameKey: PreferenceKey {
    static let defaultValue = CGRect.null
    static func reduce(value: inout CGRect, nextValue: () -> CGRect) {
        let next = nextValue()
        if !next.isNull {
            value = next
        }
    }
}

private struct BottomChromeCardFrameKey: PreferenceKey {
    static let defaultValue = CGRect.null
    static func reduce(value: inout CGRect, nextValue: () -> CGRect) {
        let next = nextValue()
        if !next.isNull {
            value = next
        }
    }
}

struct SessionRuntimeDock: View {
    let detail: SessionDetail
    var loopMode: SessionLoopMode? = nil
    var isUpdatingLoopMode: Bool = false
    var onLoopModeChange: ((SessionLoopMode) -> Void)? = nil

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
        detail.runtimeCapabilityLabel
    }

    private var accessibilityLabel: String {
        [detail.runtimeHeadline, detail.runtimeDetail, capabilityLabel]
            .compactMap { $0 }
            .joined(separator: ", ")
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
            HStack(spacing: 3) {
                Image(systemName: modeIcon)
                    .font(.caption2.weight(.semibold))
                if !typeSize.isAccessibilitySize {
                    Text(modeLabel)
                        .font(.caption2.weight(.medium))
                }
            }
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
    /// Guards against an auth-refresh→reconnect→401 loop: we attempt at most
    /// one refresh per stream session, reset once a connection succeeds.
    private var streamAuthRefreshAttempted = false
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
    private let streamFactory: (URL, String, Int?) -> SessionWorkspaceStreamSource
    private let enableRealtime: Bool
    private let transcriptCache: SessionTranscriptCache?
    /// Durable on-disk mirror of the tail; survives app eviction so a cold
    /// relaunch can hydrate before the network responds. The in-memory
    /// `transcriptCache` stays as the fast warm-resume path.
    private let snapshotStore: TranscriptSnapshotStore?
    private var lastPubsubSeq: Int?
    private let initialTailLimit = 50
    private let olderPageLimit = 50
    private let cachedTailRefreshGraceInterval: TimeInterval = 30

    init(
        apiFactory: @escaping (String) -> SessionWorkspaceClient? = { LonghouseAPI(host: $0) },
        streamFactory: @escaping (URL, String, Int?) -> SessionWorkspaceStreamSource = { baseURL, sessionId, sinceSeq in
            SessionWorkspaceStreamSource.live(baseURL: baseURL, sessionId: sessionId, sinceSeq: sinceSeq)
        },
        enableRealtime: Bool = true,
        transcriptCache: SessionTranscriptCache? = nil,
        snapshotStore: TranscriptSnapshotStore? = nil
    ) {
        self.apiFactory = apiFactory
        self.streamFactory = streamFactory
        self.enableRealtime = enableRealtime
        self.transcriptCache = transcriptCache ?? (enableRealtime ? .shared : nil)
        self.snapshotStore = snapshotStore ?? (enableRealtime ? .shared : nil)
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
            refreshErrorMessage = nil
            lastPubsubSeq = nil
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
                shouldRefreshCachedTail = Date().timeIntervalSince(snapshot.savedAt) >= cachedTailRefreshGraceInterval
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
        // (e.g. scenePhase != .active called stop()). Otherwise a scenePhase
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
        streamTask?.cancel()
        streamTask = nil
        Task { [stream] in await stream?.stop() }
        stream = nil
        streamConnected = false
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
                errorMessage = "Couldn't load session: \(error.localizedDescription)"
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
            var ticks = 0
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                if Task.isCancelled { break }
                ticks += 1
                let (connected, hasRunningTool) = await MainActor.run {
                    (
                        self?.streamConnected ?? false,
                        self?.lastWorkspaceEvents.contains { $0.toolCallState == .running } ?? false,
                    )
                }
                // Fast fallback when SSE is down. When SSE is up, the server
                // flips unpaired tool calls to "dropped" lazily on read, so
                // re-ask every ~60s while a running tool exists to keep
                // tool_call_state honest if the stream stays quiet.
                if !connected {
                    await self?.pollTick(sessionId: sessionId, appState: appState)
                } else if hasRunningTool, ticks % 12 == 0 {
                    await self?.pollTick(sessionId: sessionId, appState: appState)
                }
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
        // Seed the reconnect cursor from the persisted pubsub_seq so a fresh
        // stream (e.g. after a background pause) replays buffered events from
        // where we left off instead of cold. The server buffer is bounded
        // (~1000 msgs, process-local) with no gap signal, so this is a latency
        // optimization only — refreshTail() remains the correctness backstop.
        let s = streamFactory(base, sessionId, lastPubsubSeq)
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
        case .disconnected:
            streamConnected = false
        case .unauthorized:
            streamConnected = false
            await handleStreamUnauthorized(sessionId: sessionId, appState: appState)
        case .replayGap(let gap):
            streamConnected = true
            if gap.session_id == sessionId {
                lastPubsubSeq = gap.latest_seq > 0 ? gap.latest_seq : nil
            }
            guard let api = apiFactory(appState.serverURL) else { return }
            try? await refreshTail(api: api, sessionId: sessionId, allowFailure: true)
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
            if let transcriptPreview = change.transcript_preview?.sessionTranscriptPreview {
                applyRealtimeTranscriptPreview(transcriptPreview, sessionId: sessionId)
            }
            guard let api = apiFactory(appState.serverURL) else { return }
            try? await refreshTail(api: api, sessionId: sessionId, allowFailure: true)
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
            streamTask = nil
            stream = nil
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
        lastPubsubSeq = snapshot.lastPubsubSeq
        prefetchedOlderTail = nil
        prefetchedOlderOffset = nil
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
        prefetchedOlderTail = nil
        prefetchedOlderOffset = nil
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
            lastPubsubSeq: lastPubsubSeq
        )
        snapshotStore?.save(
            serverURL: activeServerURL,
            sessionId: activeSessionId,
            detail: storedDetail,
            events: lastWorkspaceEvents,
            loadedProjectionItemCount: loadedProjectionItemCount,
            totalProjectionItemCount: totalProjectionItemCount,
            tailSnapshotEventId: tailSnapshotEventId,
            lastPubsubSeq: lastPubsubSeq
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
            detail.project ?? "",
            detail.provider,
        ].joined(separator: "|")
    }

    var isSessionEnded: Bool {
        guard let detail else { return false }
        return detail.isClosed
    }
}
