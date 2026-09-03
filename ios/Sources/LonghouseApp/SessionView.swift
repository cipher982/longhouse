import SwiftUI
import PhotosUI
import UIKit

@MainActor
struct SessionView: View {
    let sessionId: String
    let fallbackTitle: String
    let onTranscriptDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)?
    /// Pushes a worker transcript. Owned by the navigation stack, not this view.
    var onOpenSubagent: ((String) -> Void)? = nil

    @EnvironmentObject var appState: AppState
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.openURL) private var openURL
    @StateObject private var viewModel = SessionViewModel()
    @StateObject private var liveActivityManager = SessionLiveActivityManager()
    @State private var composerText: String = ""
    @FocusState private var composerFocused: Bool
    @StateObject private var attachmentStore = ComposerAttachmentStore()
    @State private var pickerSelection: [PhotosPickerItem] = []
    @State private var isShowingPhotoPicker: Bool = false
    @State private var isLoadingPickerItems: Bool = false
    init(
        sessionId: String,
        fallbackTitle: String,
        viewModel: SessionViewModel = SessionViewModel(),
        onTranscriptDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)? = nil,
        onOpenSubagent: ((String) -> Void)? = nil
    ) {
        self.sessionId = sessionId
        self.fallbackTitle = fallbackTitle
        self.onTranscriptDiagnostics = onTranscriptDiagnostics
        self.onOpenSubagent = onOpenSubagent
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
        // Let SwiftUI's safe-area inset own both composer clearance and keyboard
        // avoidance. The previous GeometryReader -> safe-area -> DOM-padding
        // feedback loop repeatedly resized and repinned WebKit while the user was
        // trying to focus the composer.
        transcript
        .safeAreaInset(edge: .top, spacing: 0) {
            if let recap = viewModel.detail?.recap {
                SessionRecapBanner(recap: recap)
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            bottomChrome
                .frame(maxWidth: .infinity)
        }
        .navigationTitle(viewModel.detail?.displayTitle ?? fallbackTitle)
        .navigationBarTitleDisplayMode(.inline)
        .modifier(SessionNavigationSubtitle(subtitle: viewModel.detail?.identitySubtitle))
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                overflowMenu
            }
        }
        .task(id: sessionId) {
            // Navigation has already left the timeline, so starting one bounded
            // WebContent process here cannot steal the timeline's first scroll.
            // Overlap it with the tail request so existing transcripts and new
            // Console sends both reuse a ready document.
            WebTranscriptWebViewPool.prewarm()
            await viewModel.start(sessionId: sessionId, appState: appState)
            await viewModel.acknowledgeUnreadIfNeeded(
                sessionId: sessionId,
                appState: appState,
                sceneIsActive: scenePhase == .active
            )
        }
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
                Task {
                    await viewModel.start(sessionId: sessionId, appState: appState)
                    await viewModel.acknowledgeUnreadIfNeeded(
                        sessionId: sessionId,
                        appState: appState,
                        sceneIsActive: true
                    )
                }
            case .background, .inactive:
                viewModel.pauseRealtime()
            @unknown default:
                break
            }
        }
        .onChange(of: viewModel.detail?.stateFacts.lastResultAt) { previous, current in
            guard current != previous, scenePhase == .active else { return }
            Task {
                await viewModel.acknowledgeUnreadIfNeeded(
                    sessionId: sessionId,
                    appState: appState,
                    sceneIsActive: true
                )
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: UIApplication.didReceiveMemoryWarningNotification)) { _ in
            viewModel.handleMemoryWarning()
        }
        .onChange(of: viewModel.liveActivityFingerprint) { _, _ in
            guard let detail = viewModel.detail else { return }
            Task { await liveActivityManager.update(detail: detail) }
        }
        .onChange(of: viewModel.detail?.stateFacts.commitSeq) { _, _ in
            // Runtime-only catalog updates can leave the transcript payload
            // unchanged, so WebKit diagnostics are not guaranteed to fire.
            // The state view itself has rendered once SwiftUI observes the
            // canonical commit change; report that settlement independently.
            Task {
                await viewModel.recordStateRenderBeacon(
                    sessionId: sessionId,
                    appState: appState
                )
            }
        }
        .refreshable { await viewModel.reload(sessionId: sessionId, appState: appState) }
        .sheet(item: $viewModel.resumeIntent) { intent in
            ResumeCommandSheet(
                intent: intent,
                unexpectedStop: isUnexpectedResumeStop(
                    viewModel.detail?.runtimeDisplay.terminalReason
                        ?? viewModel.detail?.stateFacts.dispositionCloseReason
                )
            )
        }
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

    // One trailing glyph. The title keeps the bar; the once-per-session
    // actions (Lock Screen updates, link) live behind it.
    @ViewBuilder
    private var overflowMenu: some View {
        if let detail = viewModel.detail {
            let isWatching = liveActivityManager.isWatching(sessionId: detail.id)
            Menu {
                Button {
                    Task { await liveActivityManager.toggle(detail: detail, appState: appState) }
                } label: {
                    Label(
                        isWatching ? "Stop Lock Screen Updates" : "Lock Screen Updates",
                        systemImage: isWatching ? "bell.slash" : "bell"
                    )
                }
                .disabled(liveActivityManager.isBusy)
                Divider()
                Button {
                    UIPasteboard.general.url = sessionWebURL
                } label: {
                    Label("Copy Link", systemImage: "link")
                }
                Button {
                    if let url = sessionWebURL { openURL(url) }
                } label: {
                    Label("Open on Web", systemImage: "safari")
                }
            } label: {
                if liveActivityManager.isBusy {
                    ProgressView().controlSize(.small)
                } else {
                    Label("Session actions", systemImage: "ellipsis")
                        .labelStyle(.iconOnly)
                }
            }
            .accessibilityLabel("Session actions")
            .accessibilityIdentifier("session-overflow-menu")
        }
    }

    private var sessionWebURL: URL? {
        URL(string: appState.serverURL)?.appendingPathComponent("timeline/\(sessionId)")
    }

    @ViewBuilder
    private var runtimeDock: some View {
        if let detail = viewModel.detail {
            SessionRuntimeDock(detail: detail, activity: viewModel.activity)
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
            refreshErrorMessage: viewModel.refreshErrorMessage,
            isSyncing: viewModel.detail?.isTranscriptSyncing == true
        )
    }

    private var transcript: some View {
        let state = transcriptState
        let showTranscript = state.showsTranscript

        return ZStack {
            if showTranscript {
                WebTranscriptView(
                    serverURL: appState.serverURL,
                    items: viewModel.items,
                    subagents: viewModel.subagents,
                    submittedInputs: viewModel.submittedInputs,
                    errorMessage: viewModel.errorMessage,
                    contentRevision: viewModel.transcriptRevision,
                    sourceRevision: viewModel.benchmarkSourceRevision,
                    sourceOperation: viewModel.benchmarkSourceOperation,
                    onNearTop: {
                        Task { await viewModel.loadOlder(sessionId: sessionId, appState: appState) }
                    },
                    onNeedsMoreHistory: {
                        Task { await viewModel.fillHistoryForShortViewport(sessionId: sessionId, appState: appState) }
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
                    },
                    onOpenSubagent: onOpenSubagent
                )
                .accessibilityIdentifier("session-chat-transcript")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }

            TranscriptStateOverlay(
                state: state,
                onRetry: { Task { await viewModel.reload(sessionId: sessionId, appState: appState) } }
            )
        }
    }

    @ViewBuilder
    private var composer: some View {
        if let detail = viewModel.detail {
            if detail.activePauseRequest != nil || detail.canSendLive || detail.canDraftBeforeSendReady {
                composerField(detail: detail)
            } else {
                unavailableComposerFooter(detail: detail)
            }
        }
    }

    private func composerField(detail: SessionDetail) -> some View {
        let pauseRequest = detail.activePauseRequest
        return VStack(alignment: .leading, spacing: 6) {
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
                let sendIsEnabled = detail.canSendLive
                    && composerHasContent
                    && !viewModel.isSending
                    && !attachmentStore.isProcessing
                    && !isLoadingPickerItems
                    && !attachmentSendBlocked
                HStack(alignment: .bottom, spacing: 8) {
                    composerActionMenu(detail: detail)

                    TextField(detail.composerPlaceholder, text: $composerText, axis: .vertical)
                        .lineLimit(1...6)
                        .focused($composerFocused)
                        // Coding prompts contain paths, symbols, and identifiers that
                        // QuickType routinely rewrites. Keeping prediction out of this
                        // field also avoids doing that system layout work while the
                        // transcript WebView is settling around the keyboard.
                        .autocorrectionDisabled(true)
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
                                .foregroundStyle(sendIsEnabled ? Color(.systemBackground) : Color(.systemGray))
                                .frame(width: 30, height: 30)
                                .background(
                                    Circle().fill(sendIsEnabled
                                        ? AnyShapeStyle(Color.primary)
                                        : AnyShapeStyle(Color(.tertiarySystemFill)))
                                )
                        }
                    }
                    .disabled(!sendIsEnabled)
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

        return Menu {
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
                if attachmentIsProcessing {
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
        VStack(alignment: .leading, spacing: 8) {
            // The dock directly above already names this state. Repeating that
            // label here as a heading said the same words twice and left the
            // sentence — the only line that explains anything — as a subtitle.
            if let message = detail.controlHealthMessage {
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: detail.controlBlockIcon)
                        .font(.body)
                        .foregroundStyle(detail.isControlOffline ? .orange : .secondary)
                    Text(message)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                }
            }
            if detail.stateFacts.resume.isAvailable {
                Button {
                    Task {
                        await viewModel.prepareResume(sessionId: detail.id, appState: appState)
                    }
                } label: {
                    Label(
                        "Resume on \(detail.homeLabel ?? detail.originLabel ?? "its machine")",
                        systemImage: "terminal"
                    )
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(viewModel.isPreparingResume)
                .accessibilityIdentifier("session-resume-button")
            // Gate on the run, not the disposition. Exiting a terminal ends the
            // run but never closes the session, so this line was suppressed for
            // exactly the sessions that needed it: an ended Helm session showed
            // no Resume button and no reason why.
            } else if (detail.isClosed || detail.stateFacts.runLifecycle == "ended"),
                      detail.stateFacts.mode == "helm",
                      let reason = detail.stateFacts.resume.reason {
                Text("Resume unavailable: \(resumeReasonLabel(reason)).")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let error = viewModel.resumeErrorMessage {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
            }
            // Resume hands back a command to type on the laptop, which is the
            // wrong shape for the device this app runs on. Branching is the
            // same continuation as a text box: it starts a new session that
            // forks the provider's conversation and leaves this one alone.
            if detail.stateFacts.runLifecycle == "ended" || detail.isClosed {
                branchComposer(detail: detail)
            }
        }
        .padding(.horizontal, 4)
        .padding(.vertical, 4)
    }

    @ViewBuilder
    private func branchComposer(detail: SessionDetail) -> some View {
        BranchComposerCard(
            available: detail.stateFacts.branch.isAvailable,
            unavailableReason: detail.stateFacts.mode == "helm" ? detail.stateFacts.branch.reason : nil,
            message: $viewModel.branchMessage,
            isSubmitting: viewModel.isBranching,
            errorMessage: viewModel.branchErrorMessage,
            submit: {
                Task { await viewModel.startBranch(sessionId: detail.id, appState: appState) }
            }
        )
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
        if viewModel.detail?.canSendLive != true {
            return viewModel.detail?.controlHealthMessage ?? "Send unavailable"
        }
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
        guard viewModel.detail?.canSendLive == true else { return }
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

}

/// Identity under the title: provider · project · machine. The subtitle API
/// is iOS 26; older systems keep the plain title rather than a hand-rolled
/// principal item that fights the system bar.
private struct SessionNavigationSubtitle: ViewModifier {
    let subtitle: String?

    func body(content: Content) -> some View {
        if #available(iOS 26.0, *), let subtitle, !subtitle.isEmpty {
            content.navigationSubtitle(subtitle)
        } else {
            content
        }
    }
}

/// The provider's away recap, above the transcript: the catch-up line the
/// terminal prints in dim text when you come back. Collapsed to two lines;
/// tap to read all of it.
struct SessionRecapBanner: View {
    let recap: SessionRecap
    @State private var expanded = false

    var body: some View {
        Button {
            withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() }
        } label: {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "text.quote")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.top, 2)
                Text(recap.text)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.leading)
                    .lineLimit(expanded ? nil : 2)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(.bar)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("session-recap")
        .accessibilityLabel("Recap: \(recap.text)")
    }
}
