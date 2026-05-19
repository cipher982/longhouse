import os
import SwiftUI

#if DEBUG
private let sessionViewLogger = Logger(subsystem: "ai.longhouse.ios", category: "SessionView")
#endif

private enum TranscriptRendererMode {
    case native
    case web

    var label: String {
        switch self {
        case .native: return "Native transcript"
        case .web: return "Web transcript spike"
        }
    }

    var systemImage: String {
        switch self {
        case .native: return "text.bubble"
        case .web: return "safari"
        }
    }
}

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

    @EnvironmentObject var appState: AppState
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var viewModel = SessionViewModel()
    @StateObject private var liveActivityManager = SessionLiveActivityManager()
    @State private var composerText: String = ""
    @State private var shouldFollowTranscriptBottom = true
    @State private var transcriptUserScrollActive = false
    @State private var transcriptScrollTask: Task<Void, Never>?
    @State private var transcriptRendererMode: TranscriptRendererMode = .native
    @FocusState private var composerFocused: Bool
    private let transcriptBottomAnchorID = "session-transcript-bottom-anchor"

    init(
        sessionId: String,
        fallbackTitle: String,
        viewModel: SessionViewModel = SessionViewModel()
    ) {
        self.sessionId = sessionId
        self.fallbackTitle = fallbackTitle
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
                transcriptRendererButton
            }
            ToolbarItem(placement: .topBarTrailing) {
                watchButton
            }
        }
        .task(id: sessionId) { await viewModel.start(sessionId: sessionId, appState: appState) }
        .onDisappear {
            viewModel.stop()
            cancelTranscriptScrollTask()
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
        .onChange(of: composerFocused) { _, focused in
            debugLogTranscriptState("composer focus changed: \(focused)")
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

    private var transcriptRendererButton: some View {
        Menu {
            Button {
                transcriptRendererMode = .native
            } label: {
                Label(
                    TranscriptRendererMode.native.label,
                    systemImage: transcriptRendererMode == .native ? "checkmark.circle.fill" : TranscriptRendererMode.native.systemImage
                )
            }
            Button {
                transcriptRendererMode = .web
            } label: {
                Label(
                    TranscriptRendererMode.web.label,
                    systemImage: transcriptRendererMode == .web ? "checkmark.circle.fill" : TranscriptRendererMode.web.systemImage
                )
            }
        } label: {
            Label(transcriptRendererMode.label, systemImage: transcriptRendererMode.systemImage)
                .labelStyle(.iconOnly)
        }
        .accessibilityLabel("Transcript renderer")
        .accessibilityValue(transcriptRendererMode.label)
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
                switch transcriptRendererMode {
                case .native:
                    ScrollViewReader { proxy in
                        transcriptScroll(proxy: proxy)
                    }
                case .web:
                    WebTranscriptView(
                        items: viewModel.items,
                        submittedInputs: viewModel.submittedInputs,
                        sessionEnded: viewModel.isSessionEnded,
                        errorMessage: viewModel.errorMessage
                    )
                    .accessibilityIdentifier("session-chat-transcript-web")
                }
            }
        }
    }

    @ViewBuilder
    private func transcriptScroll(proxy: ScrollViewProxy) -> some View {
        if #available(iOS 18.0, *) {
            transcriptScrollBase
                .onScrollGeometryChange(for: Bool.self) { geometry in
                    isTranscriptGeometryAtBottom(geometry)
                } action: { _, isAtBottom in
                    updateTranscriptFollowState(isAtBottom: isAtBottom, userDriven: transcriptUserScrollActive)
                }
                .onScrollPhaseChange { _, newPhase, context in
                    updateTranscriptScrollPhase(newPhase, geometry: context.geometry)
                }
                .onChange(of: viewModel.transcriptScrollToken) { _, _ in
                    followTranscriptBottomIfNeeded(proxy, animated: false)
                }
                .onChange(of: viewModel.submittedRevealCounter) { _, _ in
                    shouldFollowTranscriptBottom = true
                    scrollTranscriptToBottom(proxy)
                }
        } else {
            GeometryReader { viewport in
                transcriptScrollBase
                    .coordinateSpace(name: "sessionTranscriptScroll")
                    .onPreferenceChange(TranscriptBottomYKey.self) { bottomY in
                        updateTranscriptFollowState(
                            isAtBottom: bottomY <= viewport.size.height + 96,
                            userDriven: true
                        )
                    }
                    .onChange(of: viewModel.transcriptScrollToken) { _, _ in
                        followTranscriptBottomIfNeeded(proxy, animated: false)
                    }
                    .onChange(of: viewModel.submittedRevealCounter) { _, _ in
                        shouldFollowTranscriptBottom = true
                        scrollTranscriptToBottom(proxy)
                    }
            }
        }
    }

    private var transcriptScrollBase: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 10) {
                if let error = viewModel.errorMessage {
                    TranscriptErrorBanner(error: error)
                }
                if viewModel.items.isEmpty && viewModel.submittedInputs.isEmpty {
                    ContentUnavailableView(
                        "No messages yet",
                        systemImage: "bubble.left.and.bubble.right"
                    )
                    .padding(.vertical, 48)
                } else {
                    transcriptItems
                }
                transcriptBottomAnchor
            }
            .padding(.horizontal)
            .padding(.vertical, 12)
        }
        .accessibilityIdentifier("session-chat-transcript")
        .scrollDismissesKeyboard(.interactively)
        .defaultScrollAnchor(.bottom)
    }

    @ViewBuilder
    private var transcriptBottomAnchor: some View {
        if #available(iOS 18.0, *) {
            Color.clear
                .frame(height: 1)
                .id(transcriptBottomAnchorID)
        } else {
            Color.clear
                .frame(height: 1)
                .background(
                    GeometryReader { marker in
                        Color.clear.preference(
                            key: TranscriptBottomYKey.self,
                            value: marker.frame(in: .named("sessionTranscriptScroll")).maxY
                        )
                    }
                )
                .id(transcriptBottomAnchorID)
        }
    }

    @ViewBuilder
    private var transcriptItems: some View {
        ForEach(viewModel.items, id: \.id) { item in
            TimelineItemView(
                item: item,
                isExpanded: viewModel.isExpanded(item.id),
                sessionEnded: viewModel.isSessionEnded,
                onToggle: { viewModel.toggleExpanded(item.id) }
            )
            .id(item.id)
        }
        ForEach(viewModel.submittedInputs) { input in
            SubmittedInputBubble(input: input) {
                composerText = input.text
                composerFocused = true
            }
            .id(input.id)
        }
    }

    private func updateTranscriptFollowState(isAtBottom: Bool, userDriven: Bool) {
        if isAtBottom {
            shouldFollowTranscriptBottom = true
        } else if userDriven {
            shouldFollowTranscriptBottom = false
            cancelTranscriptScrollTask()
        }
        debugLogTranscriptState(
            "scroll follow state: atBottom=\(isAtBottom) userDriven=\(userDriven) follow=\(shouldFollowTranscriptBottom)"
        )
    }

    @available(iOS 18.0, *)
    private func updateTranscriptScrollPhase(_ phase: ScrollPhase, geometry: ScrollGeometry) {
        let userDriven = phase == .tracking || phase == .interacting
        transcriptUserScrollActive = userDriven
        updateTranscriptFollowState(isAtBottom: isTranscriptGeometryAtBottom(geometry), userDriven: userDriven)
        debugLogTranscriptState("scroll phase: \(phase.debugDescription)")
    }

    @available(iOS 18.0, *)
    private func isTranscriptGeometryAtBottom(_ geometry: ScrollGeometry) -> Bool {
        let visibleMaxY = geometry.contentOffset.y + geometry.containerSize.height
        return visibleMaxY >= geometry.contentSize.height - 96
    }

    private func followTranscriptBottomIfNeeded(_ proxy: ScrollViewProxy, animated: Bool) {
        guard shouldFollowTranscriptBottom else {
            debugLogTranscriptState("skip follow bottom")
            return
        }
        debugLogTranscriptState("follow bottom")
        scrollTranscriptToBottom(proxy, animated: animated)
    }

    private func scrollTranscriptToBottom(
        _ proxy: ScrollViewProxy,
        animated: Bool = true
    ) {
        transcriptScrollTask?.cancel()
        transcriptScrollTask = Task { @MainActor in
            await Task.yield()
            let scroll = {
                proxy.scrollTo(transcriptBottomAnchorID, anchor: .bottom)
            }
            if animated {
                withAnimation(.easeOut(duration: 0.18), scroll)
            } else {
                scroll()
            }

            for delay in [80_000_000, 100_000_000, 140_000_000] as [UInt64] {
                try? await Task.sleep(nanoseconds: delay)
                guard !Task.isCancelled, shouldFollowTranscriptBottom else { return }
                proxy.scrollTo(transcriptBottomAnchorID, anchor: .bottom)
            }
        }
    }

    private func cancelTranscriptScrollTask() {
        transcriptScrollTask?.cancel()
        transcriptScrollTask = nil
    }

    private func debugLogTranscriptState(_ message: String) {
        #if DEBUG
        sessionViewLogger.debug(
            "\(message, privacy: .public) items=\(viewModel.items.count) submitted=\(viewModel.submittedInputs.count) follow=\(shouldFollowTranscriptBottom) userScroll=\(transcriptUserScrollActive)"
        )
        #endif
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

private struct TranscriptErrorBanner: View {
    let error: String

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle")
                .foregroundStyle(.orange)
            Text(error)
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(10)
        .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
        .accessibilityIdentifier("session-chat-error-banner")
    }
}

private struct TranscriptBottomYKey: PreferenceKey {
    static let defaultValue: CGFloat = .greatestFiniteMagnitude

    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = nextValue()
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

// MARK: - Timeline items

private struct TimelineItemView: View {
    let item: TimelineItem
    let isExpanded: Bool
    let sessionEnded: Bool
    let onToggle: () -> Void

    var body: some View {
        switch item {
        case .user(let event):
            UserBubble(event: event)
        case .assistant(let event):
            AssistantBubble(event: event)
        case .tool(let call, let result, _):
            ToolRow(call: call, result: result, isExpanded: isExpanded, sessionEnded: sessionEnded, onToggle: onToggle)
        case .orphanTool(let event):
            ToolRow(call: event, result: event, isExpanded: isExpanded, sessionEnded: sessionEnded, onToggle: onToggle, orphan: true)
        case .passiveGroup(let calls):
            PassiveGroupRow(calls: calls, isExpanded: isExpanded, onToggle: onToggle)
        }
    }
}

private struct UserBubble: View {
    let event: SessionEvent
    @State private var expanded = false

    private var text: String { event.contentText ?? "" }
    private var shouldCollapse: Bool { TranscriptTextPolicy.shouldCollapseMessage(text) }
    private var visibleText: String { TranscriptTextPolicy.visibleMessage(text, expanded: expanded) }

    var body: some View {
        HStack {
            Spacer(minLength: 40)
            VStack(alignment: .trailing, spacing: 6) {
                Text(visibleText)
                    .font(.callout)
                    .textSelection(.enabled)
                    .padding(10)
                    .background(Color.blue.opacity(0.15), in: RoundedRectangle(cornerRadius: 12))
                    .frame(alignment: .trailing)

                if shouldCollapse {
                    TranscriptExpandButton(expanded: expanded) {
                        expanded.toggle()
                    }
                }
            }
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

private struct SubmittedInputBubble: View {
    let input: SubmittedInput
    let onEdit: () -> Void

    private var statusText: String {
        switch input.phase {
        case .submitting:
            return "Sending..."
        case .sent:
            return "Sent"
        case .queued:
            return "Queued"
        case .failed:
            return input.lastError ?? "Could not send"
        case .needsUserDecision:
            return "Needs choice"
        }
    }

    private var statusColor: Color {
        switch input.phase {
        case .failed, .needsUserDecision:
            return .orange
        case .queued:
            return .secondary
        case .submitting, .sent:
            return .secondary
        }
    }

    var body: some View {
        HStack {
            Spacer(minLength: 40)
            VStack(alignment: .trailing, spacing: 6) {
                Text(input.text)
                    .font(.callout)
                    .textSelection(.enabled)
                    .padding(10)
                    .background(Color.blue.opacity(0.10), in: RoundedRectangle(cornerRadius: 12))
                    .overlay(
                        RoundedRectangle(cornerRadius: 12)
                            .stroke(Color.blue.opacity(0.20), lineWidth: 1)
                    )
                    .frame(alignment: .trailing)
                HStack(spacing: 8) {
                    if input.phase == .submitting {
                        ProgressView().controlSize(.mini)
                    }
                    Text(statusText)
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(statusColor)
                    if input.phase == .failed {
                        Button("Edit") { onEdit() }
                            .font(.caption2.weight(.semibold))
                            .buttonStyle(.plain)
                            .foregroundStyle(.blue)
                    }
                }
            }
        }
        .accessibilityIdentifier("session-chat-submitted-input")
    }
}

private struct AssistantBubble: View {
    let event: SessionEvent
    @State private var expanded = false

    private var text: String { event.contentText ?? "" }
    private var shouldCollapse: Bool { TranscriptTextPolicy.shouldCollapseMessage(text) }
    private var visibleText: String { TranscriptTextPolicy.visibleMessage(text, expanded: expanded) }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            MarkdownText(visibleText)
                .frame(maxWidth: .infinity, alignment: .leading)

            if shouldCollapse {
                TranscriptExpandButton(expanded: expanded) {
                    expanded.toggle()
                }
            }
        }
        .padding(10)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 10))
    }
}

private struct TranscriptExpandButton: View {
    let expanded: Bool
    let onToggle: () -> Void

    var body: some View {
        Button(action: onToggle) {
            Text(expanded ? "Collapse message" : "Show full message")
                .font(.caption.weight(.semibold))
        }
        .buttonStyle(.plain)
        .foregroundStyle(.blue)
        .accessibilityLabel(expanded ? "Collapse message" : "Show full message")
    }
}

/// Lightweight markdown renderer for assistant prose. Splits on fenced code
/// blocks, then applies inline markdown (bold/italic/code/links) via
/// AttributedString. Headings (## / #) and list bullets are styled manually.
private struct MarkdownText: View {
    let raw: String
    init(_ raw: String) { self.raw = raw }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                switch block {
                case .code(let text):
                    Text(text)
                        .font(.caption.monospaced())
                        .foregroundStyle(.primary)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                        .background(Color(.tertiarySystemBackground), in: RoundedRectangle(cornerRadius: 6))
                case .heading(let level, let text):
                    Text(inlineMarkdown(text))
                        .font(level == 1 ? .title3.weight(.bold) : .callout.weight(.semibold))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                case .bullet(let text):
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("•").font(.callout).foregroundStyle(.secondary)
                        Text(inlineMarkdown(text))
                            .font(.callout)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                case .paragraph(let text):
                    Text(inlineMarkdown(text))
                        .font(.callout)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    private enum Block {
        case paragraph(String)
        case heading(Int, String)
        case bullet(String)
        case code(String)
    }

    private var blocks: [Block] {
        var out: [Block] = []
        let lines = raw.components(separatedBy: "\n")
        var i = 0
        var paragraphBuffer: [String] = []

        func flushParagraph() {
            guard !paragraphBuffer.isEmpty else { return }
            let joined = paragraphBuffer.joined(separator: "\n")
            if !joined.trimmingCharacters(in: .whitespaces).isEmpty {
                out.append(.paragraph(joined))
            }
            paragraphBuffer.removeAll()
        }

        while i < lines.count {
            let line = lines[i]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if trimmed.hasPrefix("```") {
                flushParagraph()
                var code: [String] = []
                i += 1
                while i < lines.count {
                    let inner = lines[i]
                    if inner.trimmingCharacters(in: .whitespaces).hasPrefix("```") { break }
                    code.append(inner)
                    i += 1
                }
                out.append(.code(code.joined(separator: "\n")))
                i += 1
                continue
            }

            if trimmed.hasPrefix("## ") {
                flushParagraph()
                out.append(.heading(2, String(trimmed.dropFirst(3))))
                i += 1
                continue
            }
            if trimmed.hasPrefix("# ") {
                flushParagraph()
                out.append(.heading(1, String(trimmed.dropFirst(2))))
                i += 1
                continue
            }
            if trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") {
                flushParagraph()
                out.append(.bullet(String(trimmed.dropFirst(2))))
                i += 1
                continue
            }
            if trimmed.isEmpty {
                flushParagraph()
                i += 1
                continue
            }
            paragraphBuffer.append(line)
            i += 1
        }
        flushParagraph()
        return out
    }

    private func inlineMarkdown(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(
            interpretedSyntax: .inlineOnlyPreservingWhitespace
        )
        if let attr = try? AttributedString(markdown: text, options: options) {
            return attr
        }
        return AttributedString(text)
    }
}

private struct ToolRow: View {
    let call: SessionEvent
    let result: SessionEvent?
    let isExpanded: Bool
    let sessionEnded: Bool
    let onToggle: () -> Void
    var orphan: Bool = false

    private var summary: String { TimelineBuilder.inputSummary(for: call) }
    private var toolName: String { call.toolName ?? (orphan ? "Tool" : "Tool") }
    /// Pending = truly still running. A missing result becomes "dropped" once
    /// the session ends or the call is older than the age threshold.
    private var isPending: Bool {
        guard result == nil, !orphan else { return false }
        return !TimelineBuilder.isDropped(call: call, sessionEnded: sessionEnded)
    }
    private var isDropped: Bool {
        result == nil && !orphan && TimelineBuilder.isDropped(call: call, sessionEnded: sessionEnded)
    }
    private var durationText: String? {
        guard let result else { return nil }
        return TimelineBuilder.durationSeconds(call: call, result: result).map(TimelineBuilder.formatDuration)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button(action: onToggle) {
                HStack(spacing: 8) {
                    Image(systemName: icon)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .frame(width: 16)
                    Text(toolName)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.primary)
                    if !summary.isEmpty {
                        Text(summary)
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                    Spacer(minLength: 4)
                    if isPending {
                        ProgressView().controlSize(.mini)
                    } else if isDropped {
                        Text("dropped")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                            .italic()
                    } else if let durationText {
                        Text(durationText)
                            .font(.caption2.monospaced())
                            .foregroundStyle(.tertiary)
                    }
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
            }
            .buttonStyle(.plain)

            if isExpanded {
                Divider().padding(.horizontal, 10)
                VStack(alignment: .leading, spacing: 8) {
                    if !summary.isEmpty || call.toolInputJSON != nil {
                        SectionLabel("Input")
                        Text(inputBody)
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if let output = result?.toolOutputText, !output.isEmpty {
                        SectionLabel("Output")
                        Text(truncate(output))
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    } else if isPending {
                        Text("Running…").font(.caption).foregroundStyle(.tertiary)
                    } else if isDropped {
                        Text("No result recorded — likely dropped during ingest.")
                            .font(.caption).foregroundStyle(.tertiary)
                    }
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
            }
        }
        .background(Color.purple.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(
                    isPending ? Color.purple.opacity(0.4) : Color.clear,
                    style: StrokeStyle(lineWidth: 1, dash: [3, 3])
                )
        )
    }

    private var icon: String {
        switch call.toolName {
        case "Bash": return "terminal"
        case "Grep", "Glob": return "magnifyingglass"
        case "Read": return "doc.text"
        case "Edit", "Write", "NotebookEdit": return "pencil"
        case "Task": return "square.stack.3d.up"
        case "WebFetch", "WebSearch": return "globe"
        default: return "wrench.adjustable"
        }
    }

    private var inputBody: String {
        if let json = call.toolInputJSON, !json.isEmpty {
            return prettyJSON(json)
        }
        return summary
    }

    private func prettyJSON(_ value: [String: JSONValue]) -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        if let data = try? encoder.encode(value),
           let str = String(data: data, encoding: .utf8) {
            return str
        }
        return summary
    }

    private func truncate(_ text: String) -> String {
        let max = 2000
        if text.count <= max { return text }
        return String(text.prefix(max)) + "\n… (truncated)"
    }
}

/// Collapsed row for a run of passive tool calls within a turn. Shows a
/// one-line summary like `Read × 3, Grep × 2` and expands to a list of the
/// individual calls. Cuts scroll noise on Claude sessions where ~80% of
/// calls are passive reads/searches.
private struct PassiveGroupRow: View {
    let calls: [PassiveCall]
    let isExpanded: Bool
    let onToggle: () -> Void

    private var tallyText: String {
        var counts: [(String, Int)] = []
        for passive in calls {
            let name = passive.call.toolName ?? "Tool"
            if let idx = counts.firstIndex(where: { $0.0 == name }) {
                counts[idx].1 += 1
            } else {
                counts.append((name, 1))
            }
        }
        return counts.map { "\($0.0) × \($0.1)" }.joined(separator: ", ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button(action: onToggle) {
                HStack(spacing: 8) {
                    Image(systemName: "eye")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .frame(width: 16)
                    Text("Explored")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.primary)
                    Text(tallyText)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                    Spacer(minLength: 4)
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
            }
            .buttonStyle(.plain)

            if isExpanded {
                Divider().padding(.horizontal, 10)
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(calls) { passive in
                        HStack(spacing: 6) {
                            Text(passive.call.toolName ?? "Tool")
                                .font(.caption.weight(.medium))
                                .foregroundStyle(.primary)
                            let summary = TimelineBuilder.inputSummary(for: passive.call)
                            if !summary.isEmpty {
                                Text(summary)
                                    .font(.caption.monospaced())
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                            }
                            Spacer(minLength: 0)
                        }
                    }
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
            }
        }
        .background(Color.gray.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
    }
}

private struct SectionLabel: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text.uppercased())
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.tertiary)
            .tracking(0.5)
    }
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
    /// Most recent send outcome so the UI can distinguish an immediate
    /// dispatch from a queued input without pretending the latter was sent.
    @Published var lastSendOutcome: SessionInputOutcome?
    @Published var queuedInputCount: Int = 0
    @Published var failedInputCount: Int = 0
    @Published var submittedInputs: [SubmittedInput] = []
    @Published private(set) var submittedRevealCounter: UInt64 = 0
    /// Text preserved from a steer attempt that the server rejected with
    /// error_code: "turn_ended". The UI offers an explicit "Queue instead"
    /// action; we do not silently convert the intent for the user.
    @Published var turnEndedDraft: String?
    /// Monotonic counter; each send increments it. Used so a delayed "Sent."
    /// auto-dismiss task only clears the label it owns.
    private(set) var sendCounter: UInt64 = 0

    private var expandedIds: Set<String> = []
    private var pollTask: Task<Void, Never>?
    private var stream: SessionWorkspaceStreamSource?
    private var streamTask: Task<Void, Never>?
    private var streamConnected: Bool = false
    private var activeSessionId: String?
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

    func isExpanded(_ id: String) -> Bool { expandedIds.contains(id) }

    var transcriptScrollToken: String {
        let itemPart = items.suffix(3)
            .map(transcriptItemSignature)
            .joined(separator: "\u{1F}")
        let submittedPart = submittedInputs
            .map { input in
                [
                    input.id,
                    input.phase.rawValue,
                    input.serverInputId.map(String.init) ?? "local",
                ].joined(separator: "\u{1E}")
            }
            .joined(separator: "\u{1F}")
        return "\(items.count)\u{1D}\(itemPart)\u{1D}\(submittedPart)"
    }

    private func transcriptItemSignature(_ item: TimelineItem) -> String {
        switch item {
        case .user(let event), .assistant(let event), .orphanTool(let event):
            return eventSignature(item.id, event)
        case .tool(let call, let result, _):
            return [
                item.id,
                eventSignature("call", call),
                result.map { eventSignature("result", $0) } ?? "pending",
            ].joined(separator: "\u{1E}")
        case .passiveGroup(let calls):
            let callPart = calls.suffix(3).map { passive in
                [
                    eventSignature("call", passive.call),
                    passive.result.map { eventSignature("result", $0) } ?? "pending",
                ].joined(separator: "\u{1C}")
            }.joined(separator: "\u{1E}")
            return "passive\u{1E}\(calls.count)\u{1E}\(callPart)"
        }
    }

    private func eventSignature(_ prefix: String, _ event: SessionEvent) -> String {
        [
            prefix,
            String(event.id),
            event.timestamp,
            String(event.contentText?.count ?? 0),
            String(event.toolOutputText?.count ?? 0),
        ].joined(separator: "\u{1C}")
    }

    func toggleExpanded(_ id: String) {
        if expandedIds.contains(id) { expandedIds.remove(id) } else { expandedIds.insert(id) }
        objectWillChange.send()
    }

    func start(sessionId: String, appState: AppState) async {
        let sessionChanged = activeSessionId != sessionId
        if sessionChanged {
            activeSessionId = sessionId
            isInitialLoading = true
            detail = nil
            items = []
            submittedInputs = []
            errorMessage = nil
            expandedIds.removeAll()
        }
        if isInitialLoading {
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
        submittedRevealCounter &+= 1
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

    func dismissSubmittedInput(_ id: String) {
        submittedInputs.removeAll { $0.id == id }
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
            self.items = TimelineBuilder.build(events: events)
            reconcileSubmittedInputs(with: events)
            Task { [weak self] in
                guard let self else { return }
                await self.reportRenderBeacon(api: api, sessionId: sessionId, events: events)
            }
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
        let now = Date()
        submittedInputs.removeAll { input in
            guard input.phase == .sent || input.phase == .queued || input.phase == .submitting else { return false }
            let age = now.timeIntervalSince(input.createdAt)
            return events.contains { event in
                guard event.role == "user", event.contentText == input.text else { return false }
                guard let eventDate = LonghouseDateParser.parse(event.timestamp) else { return false }
                // Near-realtime window: tight [-5s, +120s] around submit.
                if eventDate >= input.createdAt.addingTimeInterval(-5)
                    && eventDate <= input.createdAt.addingTimeInterval(120) {
                    return true
                }
                // Late-echo fallback: after the tight window has passed, accept any
                // durable user event with matching text from >=createdAt-5s. This
                // prevents a stale optimistic row sitting next to a duplicate
                // transcript row when ingest was delayed past 120s.
                if age > 120 && eventDate >= input.createdAt.addingTimeInterval(-5) {
                    return true
                }
                return false
            }
        }
    }

    private func reportRenderBeacon(api: SessionWorkspaceClient, sessionId: String, events: [SessionEvent]) async {
        guard let latest = events.last else { return }
        guard let emittedAt = LonghouseDateParser.parse(latest.timestamp) else { return }
        let caps = detail?.capabilities
        let managed = (caps?.liveControlAvailable == true) || (caps?.hostReattachAvailable == true)
        if let payload = await RenderBeaconReporter.shared.payload(
            sessionId: sessionId,
            latestEventId: String(latest.id),
            emittedAt: emittedAt,
            managed: managed
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
