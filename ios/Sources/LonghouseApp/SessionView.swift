import SwiftUI

@MainActor
struct SessionView: View {
    let sessionId: String
    let fallbackTitle: String

    @EnvironmentObject var appState: AppState
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var viewModel = SessionViewModel()
    @StateObject private var liveActivityManager = SessionLiveActivityManager()
    @State private var composerText: String = ""
    @FocusState private var composerFocused: Bool

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
        .onDisappear { viewModel.stop() }
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
            SessionRuntimeDock(detail: detail)
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
            } else if let error = viewModel.errorMessage, viewModel.items.isEmpty {
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
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 10) {
                            if viewModel.items.isEmpty {
                                ContentUnavailableView(
                                    "No messages yet",
                                    systemImage: "bubble.left.and.bubble.right"
                                )
                                .padding(.vertical, 48)
                            } else {
                                ForEach(viewModel.items, id: \.id) { item in
                                    TimelineItemView(
                                        item: item,
                                        isExpanded: viewModel.isExpanded(item.id),
                                        sessionEnded: viewModel.isSessionEnded,
                                        onToggle: { viewModel.toggleExpanded(item.id) }
                                    )
                                    .id(item.id)
                                }
                            }
                        }
                        .padding(.horizontal)
                        .padding(.vertical, 12)
                    }
                    .onChange(of: viewModel.items.count) { _, _ in
                        if let last = viewModel.items.last {
                            withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                        }
                    }
                    .onAppear {
                        if let last = viewModel.items.last {
                            DispatchQueue.main.async {
                                proxy.scrollTo(last.id, anchor: .bottom)
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var composer: some View {
        if let detail = viewModel.detail {
            if detail.capabilities.liveControlAvailable || detail.capabilities.replyToLiveSessionAvailable {
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

            HStack(spacing: 8) {
                LoopModeButtons(
                    currentMode: detail.effectiveLoopMode,
                    disabled: viewModel.isUpdatingLoopMode,
                    onChange: { mode in
                        Task {
                            await viewModel.setLoopMode(sessionId: sessionId, mode: mode, appState: appState)
                        }
                    }
                )
                if viewModel.isUpdatingLoopMode {
                    ProgressView()
                        .controlSize(.mini)
                }
                Spacer(minLength: 0)
            }
            .accessibilityIdentifier("session-loop-mode-controls")

            if let loopModeErrorMessage = viewModel.loopModeErrorMessage {
                Text(loopModeErrorMessage)
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            HStack(alignment: .bottom, spacing: 8) {
                Button {
                    Task { await draft() }
                } label: {
                    if viewModel.isDrafting {
                        ProgressView()
                            .controlSize(.small)
                    } else {
                        Image(systemName: "sparkles")
                            .font(.title3)
                    }
                }
                .frame(width: 32, height: 32)
                .disabled(composerHasText || viewModel.isSending || viewModel.isDrafting)
                .accessibilityLabel("Draft reply")
                .accessibilityHint("Available when the reply field is empty")

                TextField("Reply", text: $composerText, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...6)
                    .focused($composerFocused)
                    .disabled(viewModel.isSending || viewModel.isDrafting)

                if showSecondaryQueueAction {
                    Button("Queue next") {
                        Task { await send(intent: "queue") }
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .disabled(!composerHasText || viewModel.isSending || viewModel.isDrafting)
                    .accessibilityLabel("Queue next message")
                }

                if primaryIntent == "auto" {
                    Button {
                        Task { await send() }
                    } label: {
                        if viewModel.isSending {
                            ProgressView()
                        } else {
                            Image(systemName: "arrow.up.circle.fill")
                                .font(.title2)
                        }
                    }
                    .disabled(!composerHasText || viewModel.isSending || viewModel.isDrafting)
                    .accessibilityLabel("Send reply")
                } else {
                    Button(primaryIntent == "steer" ? "Send update" : "Queue next") {
                        Task { await send() }
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                    .disabled(!composerHasText || viewModel.isSending || viewModel.isDrafting)
                    .accessibilityLabel(primaryIntent == "steer" ? "Send update mid-turn" : "Queue next message")
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
        guard let detail = viewModel.detail, detail.isSessionExecuting else { return "auto" }
        if detail.canSteerActiveTurn { return "steer" }
        if detail.canQueueNextInput { return "queue" }
        return "auto"
    }

    private var showSecondaryQueueAction: Bool {
        guard let detail = viewModel.detail else { return false }
        return detail.isSessionExecuting && detail.canSteerActiveTurn && detail.canQueueNextInput
    }

    private func send(intent: String? = nil) async {
        let trimmed = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        let resolvedIntent = intent ?? primaryIntent
        let sent = await viewModel.send(
            text: trimmed,
            sessionId: sessionId,
            appState: appState,
            intent: resolvedIntent,
        )
        if sent {
            composerText = ""
            composerFocused = false
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

private struct SessionRuntimeDock: View {
    let detail: SessionDetail

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
        case "needs-user": return "exclamationmark.circle"
        case "blocked": return "lock.circle"
        case "idle": return "checkmark.circle"
        case "inferred": return "waveform.path"
        default: return "circle"
        }
    }

    private var toneColor: Color {
        switch detail.runtimeTone {
        case "running", "thinking": return .green
        case "needs-user": return .yellow
        case "blocked": return .orange
        case "idle": return .gray
        case "inferred": return .blue
        default: return .gray
        }
    }
}

private struct LoopModeButtons: View {
    let currentMode: SessionLoopMode
    let disabled: Bool
    let onChange: (SessionLoopMode) -> Void

    var body: some View {
        HStack(spacing: 6) {
            modeButton(.assist, title: "Assist", systemImage: "wand.and.stars")
            modeButton(.autopilot, title: "Autopilot", systemImage: "bolt.circle")
            Menu {
                Button {
                    onChange(.manual)
                } label: {
                    Label("Turn off assistance", systemImage: "pause.circle")
                }
            } label: {
                Image(systemName: currentMode == .manual ? "ellipsis.circle.fill" : "ellipsis.circle")
                    .font(.title3)
                    .accessibilityLabel("More control modes")
            }
            .disabled(disabled || currentMode == .manual)
        }
    }

    private func modeButton(_ mode: SessionLoopMode, title: String, systemImage: String) -> some View {
        Button {
            onChange(mode)
        } label: {
            Label(title, systemImage: systemImage)
        }
        .buttonStyle(.bordered)
        .tint(currentMode == mode ? Color.accentColor : Color.secondary)
        .controlSize(.small)
        .disabled(disabled || currentMode == mode)
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
        case .tool(let call, let result):
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
    /// Text preserved from a steer attempt that the server rejected with
    /// error_code: "turn_ended". The UI offers an explicit "Queue instead"
    /// action; we do not silently convert the intent for the user.
    @Published var turnEndedDraft: String?
    /// Monotonic counter; each send increments it. Used so a delayed "Sent."
    /// auto-dismiss task only clears the label it owns.
    private(set) var sendCounter: UInt64 = 0

    private var expandedIds: Set<String> = []
    private var pollTask: Task<Void, Never>?
    private var stream: SessionWorkspaceStream?
    private var streamTask: Task<Void, Never>?
    private var streamConnected: Bool = false

    func isExpanded(_ id: String) -> Bool { expandedIds.contains(id) }

    func toggleExpanded(_ id: String) {
        if expandedIds.contains(id) { expandedIds.remove(id) } else { expandedIds.insert(id) }
        objectWillChange.send()
    }

    func start(sessionId: String, appState: AppState) async {
        if isInitialLoading {
            await reload(sessionId: sessionId, appState: appState)
        }
        startStream(sessionId: sessionId, appState: appState)
        startVisiblePolling(sessionId: sessionId, appState: appState)
    }

    func stop() {
        pollTask?.cancel()
        pollTask = nil
        streamTask?.cancel()
        streamTask = nil
        Task { [stream] in await stream?.stop() }
        stream = nil
        streamConnected = false
    }

    func reload(sessionId: String, appState: AppState) async {
        guard let api = LonghouseAPI(host: appState.serverURL) else {
            errorMessage = "Invalid server URL"
            isInitialLoading = false
            return
        }
        do {
            async let detailTask = api.sessionDetail(id: sessionId)
            async let eventsTask = api.sessionEvents(id: sessionId)
            let (detail, events) = try await (detailTask, eventsTask)
            self.detail = detail
            self.items = TimelineBuilder.build(events: events)
            self.errorMessage = nil
            self.loopModeErrorMessage = nil
            await reportRenderBeacon(api: api, sessionId: sessionId, events: events)
        } catch LonghouseAPIError.notAuthenticated {
            errorMessage = "Session expired."
        } catch {
            errorMessage = "Couldn't load session: \(error.localizedDescription)"
        }
        isInitialLoading = false
    }

    func send(text: String, sessionId: String, appState: AppState, intent: String = "auto") async -> Bool {
        guard let api = LonghouseAPI(host: appState.serverURL) else { return false }
        isSending = true
        draftErrorMessage = nil
        defer { isSending = false }
        do {
            let response = try await api.sendInput(id: sessionId, text: text, intent: intent)
            sendCounter &+= 1
            lastSendOutcome = response.outcome
            queuedInputCount = response.pendingInputCount
            failedInputCount = response.visibleFailedInputCount
            turnEndedDraft = nil
            if response.outcome == .sent, let events = try? await api.sessionEvents(id: sessionId) {
                self.items = TimelineBuilder.build(events: events)
            }
            return true
        } catch let LonghouseAPIError.structured(_, code, message) where intent == "steer" && code == "turn_ended" {
            // Preserve the original text; the UI offers an explicit
            // "Queue instead" action. Intent is never silently mapped.
            turnEndedDraft = text
            errorMessage = message.isEmpty ? "Active turn ended before your update arrived." : message
            return false
        } catch {
            errorMessage = "Send failed: \(error.localizedDescription)"
            return false
        }
    }

    /// Explicit user acceptance of the "Queue instead" prompt after a
    /// steer failed with turn_ended. Always maps to intent=queue.
    func queueInsteadOfSteer(sessionId: String, appState: AppState) async -> Bool {
        guard let text = turnEndedDraft else { return false }
        turnEndedDraft = nil
        return await send(text: text, sessionId: sessionId, appState: appState, intent: "queue")
    }

    func draftReply(sessionId: String, appState: AppState) async -> String? {
        guard let api = LonghouseAPI(host: appState.serverURL) else { return nil }
        isDrafting = true
        draftErrorMessage = nil
        defer { isDrafting = false }
        do {
            let response = try await api.draftReply(id: sessionId)
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
        guard let api = LonghouseAPI(host: appState.serverURL) else { return }
        isUpdatingLoopMode = true
        loopModeErrorMessage = nil
        defer { isUpdatingLoopMode = false }
        do {
            _ = try await api.setSessionLoopMode(id: sessionId, loopMode: mode)
            detail = try await api.sessionDetail(id: sessionId)
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
        streamTask?.cancel()
        guard let base = URL(string: appState.serverURL) else { return }
        let s = SessionWorkspaceStream(baseURL: base, sessionId: sessionId)
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
            guard let api = LonghouseAPI(host: appState.serverURL) else { return }
            async let detailTask = api.sessionDetail(id: sessionId)
            async let eventsTask = api.sessionEvents(id: sessionId)
            if let detail = try? await detailTask {
                self.detail = detail
            }
            if let events = try? await eventsTask {
                self.items = TimelineBuilder.build(events: events)
                await reportRenderBeacon(api: api, sessionId: sessionId, events: events)
            }
        }
    }

    private func pollTick(sessionId: String, appState: AppState) async {
        guard let api = LonghouseAPI(host: appState.serverURL) else { return }
        async let detailTask = api.sessionDetail(id: sessionId)
        async let eventsTask = api.sessionEvents(id: sessionId)
        if let detail = try? await detailTask {
            self.detail = detail
        }
        if let events = try? await eventsTask {
            self.items = TimelineBuilder.build(events: events)
            await reportRenderBeacon(api: api, sessionId: sessionId, events: events)
        }
    }

    private func reportRenderBeacon(api: LonghouseAPI, sessionId: String, events: [SessionEvent]) async {
        guard let latest = events.last else { return }
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let isoFallback = ISO8601DateFormatter()
        isoFallback.formatOptions = [.withInternetDateTime]
        guard let emittedAt = iso.date(from: latest.timestamp) ?? isoFallback.date(from: latest.timestamp) else { return }
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

    var isSessionActive: Bool {
        guard let detail else { return false }
        let active: Set<String> = ["running", "thinking", "working", "needs_user", "blocked", "active"]
        if let presence = detail.presenceState, active.contains(presence) { return true }
        if let status = detail.status, active.contains(status) { return true }
        return false
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

    /// Phase 3 of session-liveness-honesty: trust only the backend's
    /// three-axis lifecycle truth when present. Legacy fallback reads
    /// presence/status — historically both were set off parser-derived
    /// ended_at, so they've lied about closure for unmanaged sessions.
    /// The backend now only emits status="completed" when there's a real
    /// terminal signal, so this fallback is safe going forward.
    var isSessionEnded: Bool {
        guard let detail else { return false }
        if let lifecycle = detail.runtimeDisplay?.lifecycle {
            return lifecycle == "closed"
        }
        let terminal: Set<String> = ["completed", "closed", "ended", "terminated"]
        if let presence = detail.presenceState, terminal.contains(presence) { return true }
        if let status = detail.status, terminal.contains(status) { return true }
        return false
    }
}
