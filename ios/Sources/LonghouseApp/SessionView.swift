import SwiftUI
import PhotosUI
import UIKit

@MainActor
struct SessionView: View {
    let sessionId: String
    let fallbackTitle: String
    let fallbackSubtitle: String?
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
        fallbackSubtitle: String? = nil,
        viewModel: SessionViewModel = SessionViewModel(),
        onTranscriptDiagnostics: ((RenderBeaconReporter.WebKitDiagnostics) -> Void)? = nil,
        onOpenSubagent: ((String) -> Void)? = nil
    ) {
        self.sessionId = sessionId
        self.fallbackTitle = fallbackTitle
        self.fallbackSubtitle = fallbackSubtitle
        self.onTranscriptDiagnostics = onTranscriptDiagnostics
        self.onOpenSubagent = onOpenSubagent
        _viewModel = StateObject(wrappedValue: viewModel)
    }

    private var attachmentInputEnabled: Bool {
        guard let detail = viewModel.detail else { return false }
        return SessionComposerControlState.attachmentInputEnabled(for: detail)
    }

    var body: some View {
        // Let SwiftUI's safe-area inset own both composer clearance and keyboard
        // avoidance. The previous GeometryReader -> safe-area -> DOM-padding
        // feedback loop repeatedly resized and repinned WebKit while the user was
        // trying to focus the composer.
        transcript
        .safeAreaInset(edge: .top, spacing: 0) {
            // A bar over a scrolling transcript needs its own bottom edge, or
            // the text sliding under it reads as part of the banner.
            if let recap = viewModel.detail?.recap {
                VStack(spacing: 0) {
                    SessionRecapBanner(recap: recap, usage: viewModel.detail?.usageLatest)
                    Divider()
                }
            } else if let usage = viewModel.detail?.usageLatest {
                VStack(spacing: 0) {
                    SessionUsageChip(usage: usage)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                        .padding(.horizontal, 16)
                        .padding(.vertical, 4)
                        .background(.bar)
                    Divider()
                }
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            bottomChrome
                .frame(maxWidth: .infinity)
        }
        // The principal toolbar item owns the visible title surface. Keep a
        // stable fallback navigation title for the Back label; never bind the
        // navigation title to detail so it cannot resize during the push.
        .navigationTitle(fallbackTitle)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                SessionNavigationHeader(
                    title: viewModel.detail?.displayTitle ?? fallbackTitle,
                    subtitle: viewModel.detail?.identitySubtitle ?? fallbackSubtitle
                )
            }
            // Keep one trailing toolbar slot mounted for the entire push. The
            // loading glyph is the bounded placeholder; replacing its content
            // does not insert a second control over the destination title.
            ToolbarItem(placement: .topBarTrailing) {
                if isSessionInteractionReady {
                    overflowMenu
                } else if viewModel.isInitialLoading || viewModel.detail != nil {
                    Image(systemName: "ellipsis")
                        .frame(width: 32, height: 32)
                        .foregroundStyle(.secondary)
                        .accessibilityLabel("Session actions unavailable until transcript is ready")
                        .accessibilityIdentifier("session-navigation-loading")
                }
            }
        }
        .task(id: sessionId) {
            // The timeline owns the warm spare. A session route must issue its
            // primary request immediately; constructing WebKit here would
            // consume the same main-actor slice before the first network byte.
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
        .onChange(of: viewModel.isTranscriptFrameReady) { _, ready in
            guard ready, scenePhase == .active else { return }
            viewModel.transcriptFrameDidBecomeReady(sessionId: sessionId, appState: appState)
            Task {
                await viewModel.acknowledgeUnreadIfNeeded(
                    sessionId: sessionId,
                    appState: appState,
                    sceneIsActive: true
                )
            }
        }
        .onChange(of: viewModel.renderedTranscriptReadThrough) { previous, current in
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
            if isSessionInteractionReady {
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
            } else if viewModel.isInitialLoading || transcriptState == .restoring {
                SessionLoadingDock()
            }
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 10)
    }

    // One trailing glyph. The title keeps the bar; the once-per-session
    // actions (Lock Screen updates, link) live behind it.
    // Keep the toolbar slot mounted while the detail and transcript arrive.
    // Conditional insertion/removal during a NavigationStack push produces
    // the blurred ghost controls seen in the cold-open transition.
    private var overflowMenu: some View {
        Menu {
            if let detail = viewModel.detail {
                let isWatching = liveActivityManager.isWatching(sessionId: detail.id)
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
            }
            // These actions only need the route identity, so they remain
            // useful while compact metadata and transcript rows are loading.
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

    private var sessionWebURL: URL? {
        URL(string: appState.serverURL)?.appendingPathComponent("timeline/\(sessionId)")
    }

    @ViewBuilder
    private var runtimeDock: some View {
        if let detail = viewModel.detail {
            SessionRuntimeDock(
                detail: detail,
                activity: viewModel.activity,
                realtimeConnection: viewModel.realtimeConnection
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

    private var isSessionInteractionReady: Bool {
        // Primary detail can arrive before the transcript tail. Keep the
        // header responsive, but leave the composer and actions in their
        // bounded loading shell until the initial transcript lane settles.
        viewModel.detail != nil && !viewModel.isInitialLoading
    }

    private var transcriptState: TranscriptDisplayState {
        TranscriptDisplayState.derive(
            isInitialLoading: viewModel.isInitialLoading,
            hasContent: !viewModel.items.isEmpty || !viewModel.submittedInputs.isEmpty,
            errorMessage: viewModel.errorMessage,
            refreshErrorMessage: viewModel.refreshErrorMessage,
            isSyncing: viewModel.detail?.isTranscriptSyncing == true,
            rendererReady: viewModel.isTranscriptFrameReady,
            rendererErrorMessage: viewModel.transcriptRendererErrorMessage
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
                    transcriptReadThrough: viewModel.transcriptReadThrough,
                    retryRevision: viewModel.transcriptRenderRetryRevision,
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
                    onOpenSubagent: onOpenSubagent,
                    onFrameFailed: { receipt in
                        viewModel.recordTranscriptFrameFailed(receipt)
                    },
                    onFrameRendered: { receipt in
                        viewModel.recordTranscriptFrameRendered(receipt)
                    }
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                // Keep stale WebKit DOM out of VoiceOver and hit testing until
                // its current document has acknowledged a frame. The native
                // overlay is the honest loading/retry surface during this gap.
                .accessibilityHidden(!viewModel.isTranscriptFrameReady)
                .allowsHitTesting(viewModel.isTranscriptFrameReady)
                .accessibilityIdentifier("session-chat-transcript")
            }

            TranscriptStateOverlay(
                state: state,
                onRetry: {
                    // Renderer recovery is independent from REST refresh.
                    // The transcript stays mounted behind this surface, so
                    // retrying a frame must not wait on a second network call.
                    if viewModel.transcriptRendererErrorMessage != nil {
                        viewModel.prepareTranscriptRetry()
                    } else {
                        Task { await viewModel.reload(sessionId: sessionId, appState: appState) }
                    }
                }
            )
        }
    }

    @ViewBuilder
    private var composer: some View {
        if let detail = viewModel.detail {
            if SessionComposerControlState.isVisible(for: detail) {
                composerField(detail: detail)
            } else {
                unavailableComposerFooter(detail: detail)
            }
        }
    }

    private func composerField(detail: SessionDetail) -> some View {
        let pauseRequest = detail.activePauseRequest
        return SessionComposer(
            detail: detail,
            text: $composerText,
            focused: $composerFocused,
            failedInputCount: viewModel.failedInputCount,
            queuedInputCount: viewModel.queuedInputCount,
            lastSendOutcome: viewModel.lastSendOutcome,
            isSending: viewModel.isSending,
            attachmentIsEmpty: attachmentStore.isEmpty,
            attachmentIsProcessing: attachmentStore.isProcessing,
            isLoadingPickerItems: isLoadingPickerItems,
            turnEndedDraft: viewModel.turnEndedDraft,
            onQueueInstead: {
                _ = await viewModel.queueInsteadOfSteer(sessionId: sessionId, appState: appState)
            },
            onDismissTurnEnded: {
                viewModel.turnEndedDraft = nil
                viewModel.errorMessage = nil
            },
            pauseIsResponding: viewModel.isRespondingToPauseRequest,
            pauseErrorMessage: viewModel.pauseResponseErrorMessage,
            onPauseRespond: { decision, answers, content, message in
                guard let pauseRequest else { return false }
                return await viewModel.respondToPauseRequest(
                    sessionId: sessionId,
                    appState: appState,
                    pauseRequest: pauseRequest,
                    decision: decision,
                    answers: answers,
                    content: content,
                    message: message
                )
            },
            onSend: { intent in await send(intent: intent) },
            actionMenu: {
                SessionComposerActionMenu(
                    detail: detail,
                    attachmentSlotsLeft: attachmentStore.slotsLeft,
                    attachmentInputEnabled: attachmentInputEnabled,
                    isProcessing: attachmentStore.isProcessing || isLoadingPickerItems,
                    isSending: viewModel.isSending,
                    onAttach: { isShowingPhotoPicker = true }
                )
            },
            attachmentTray: { attachmentTray }
        )
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

    private func send(intent: String? = nil) async {
        guard !viewModel.isSending else { return }
        guard !attachmentStore.isProcessing else { return }
        guard !isLoadingPickerItems else { return }
        guard let detail = viewModel.detail, detail.canSendLive else { return }
        let trimmed = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
        let pendingAttachments = attachmentStore.snapshot()
        guard !trimmed.isEmpty || !pendingAttachments.isEmpty else { return }
        let requestedIntent = intent ?? SessionComposerControlState.primaryIntent(for: detail)
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

/// Constrained principal title for the session route. The system navigation
/// title can participate in the push transition with an unconstrained width;
/// keeping the title and subtitle in one bounded view prevents long titles from
/// sliding under the trailing control while the transcript is still loading.
struct SessionNavigationHeader: View {
    let title: String
    let subtitle: String?

    var body: some View {
        VStack(spacing: 0) {
            Text(title)
                .font(.headline)
                .lineLimit(1)
                .minimumScaleFactor(0.72)
                .truncationMode(.tail)
            if let subtitle, !subtitle.isEmpty {
                Text(subtitle)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.78)
                    .truncationMode(.tail)
            }
        }
        .frame(maxWidth: 200)
        .clipped()
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(
            subtitle.map { "\(title), \($0)" } ?? title
        )
        .accessibilityIdentifier("session-navigation-title")
    }
}

/// Stable native chrome shown until the primary session detail or a cache is
/// available. It keeps the route visibly finished while the transcript tail
/// continues in its own lane.
struct SessionLoadingDock: View {
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "text.bubble")
                .foregroundStyle(.secondary)
                .font(.subheadline)
            VStack(alignment: .leading, spacing: 2) {
                Text("Loading session")
                    .font(.subheadline.weight(.semibold))
                Text("Getting the latest messages")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(
            RoundedRectangle(cornerRadius: 24, style: .continuous)
                .fill(.ultraThinMaterial)
                .overlay(
                    RoundedRectangle(cornerRadius: 24, style: .continuous)
                        .strokeBorder(.white.opacity(0.10), lineWidth: 0.75)
                )
        )
        .shadow(color: .black.opacity(0.28), radius: 16, y: 5)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("session-loading-dock")
    }
}

/// The provider's away recap, above the transcript: the catch-up line the
/// terminal prints in dim text when you come back. Collapsed to two lines;
/// tap to read all of it.
struct SessionRecapBanner: View {
    let recap: SessionRecap
    var usage: SessionUsageLatest? = nil
    @State private var expanded = false

    var body: some View {
        Button {
            withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() }
        } label: {
            VStack(alignment: .leading, spacing: 6) {
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
                if let usage {
                    SessionUsageChip(usage: usage)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(.bar)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("session-recap")
        // One control: say everything it shows, and that it expands.
        .accessibilityLabel(usage.map { "Recap: \(recap.text). Model: \($0.chipLabel)" } ?? "Recap: \(recap.text)")
        .accessibilityValue(expanded ? "Expanded" : "Collapsed")
        .accessibilityHint(expanded ? "Collapses the recap" : "Expands the recap")
        .accessibilityAddTraits(.isButton)
    }
}

/// "opus 5 · high · 501k ctx": the provider's model line as a quiet chip.
struct SessionUsageChip: View {
    let usage: SessionUsageLatest

    var body: some View {
        Text(usage.chipLabel)
            .font(.caption2.monospacedDigit())
            .foregroundStyle(.secondary)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(Capsule().fill(.quaternary))
            .accessibilityIdentifier("session-usage-chip")
    }
}
