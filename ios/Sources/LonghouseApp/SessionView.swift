import SwiftUI

@MainActor
struct SessionView: View {
    let sessionId: String
    let fallbackTitle: String

    @EnvironmentObject var appState: AppState
    @StateObject private var viewModel = SessionViewModel()
    @State private var composerText: String = ""
    @FocusState private var composerFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            transcript
            composer
        }
        .navigationTitle(viewModel.detail?.displayTitle ?? fallbackTitle)
        .navigationBarTitleDisplayMode(.inline)
        .task(id: sessionId) { await viewModel.start(sessionId: sessionId, appState: appState) }
        .onDisappear { viewModel.stop() }
        .refreshable { await viewModel.reload(sessionId: sessionId, appState: appState) }
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
            } else if viewModel.items.isEmpty {
                ContentUnavailableView(
                    "No messages yet",
                    systemImage: "bubble.left.and.bubble.right"
                )
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 10) {
                            if let detail = viewModel.detail {
                                SessionHeader(detail: detail)
                            }
                            ForEach(viewModel.items, id: \.id) { item in
                                TimelineItemView(
                                    item: item,
                                    isExpanded: viewModel.isExpanded(item.id),
                                    onToggle: { viewModel.toggleExpanded(item.id) }
                                )
                                .id(item.id)
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
                }
            }
        }
    }

    @ViewBuilder
    private var composer: some View {
        if let detail = viewModel.detail {
            if detail.capabilities.liveControlAvailable || detail.capabilities.replyToLiveSessionAvailable {
                composerField(enabled: true)
            } else {
                unmanagedFooter()
            }
        }
    }

    private func composerField(enabled: Bool) -> some View {
        HStack(alignment: .bottom, spacing: 8) {
            TextField("Reply", text: $composerText, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(1...6)
                .focused($composerFocused)
                .disabled(viewModel.isSending || !enabled)
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
            .disabled(composerText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || viewModel.isSending)
        }
        .padding(12)
        .background(.bar)
    }

    private func unmanagedFooter() -> some View {
        HStack {
            Image(systemName: "info.circle")
                .foregroundStyle(.secondary)
            Text("Unmanaged session — read-only on mobile.")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
        }
        .padding(12)
        .background(.bar)
    }

    private func send() async {
        let trimmed = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        let sent = await viewModel.send(text: trimmed, sessionId: sessionId, appState: appState)
        if sent {
            composerText = ""
            composerFocused = false
        }
    }
}

// MARK: - Header

private struct SessionHeader: View {
    let detail: SessionDetail

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                PresenceBadge(state: detail.presenceState ?? detail.status ?? "idle")
                if let home = detail.homeLabel {
                    Text(home).font(.caption).foregroundStyle(.secondary)
                }
            }
            if let project = detail.project {
                Text(project).font(.caption).foregroundStyle(.secondary)
            }
            if let cwd = detail.cwd {
                Text(cwd).font(.caption2).foregroundStyle(.tertiary).lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 10))
    }
}

private struct PresenceBadge: View {
    let state: String

    var body: some View {
        HStack(spacing: 4) {
            Circle().fill(color).frame(width: 8, height: 8)
            Text(label).font(.caption.weight(.medium))
        }
        .padding(.horizontal, 8).padding(.vertical, 4)
        .background(color.opacity(0.15), in: Capsule())
        .foregroundStyle(color)
    }

    private var color: Color {
        switch state {
        case "running", "thinking", "working", "active": return .green
        case "needs_user": return .yellow
        case "blocked": return .orange
        case "idle", "completed": return .gray
        default: return .gray
        }
    }

    private var label: String {
        switch state {
        case "running": return "Running"
        case "thinking": return "Thinking"
        case "needs_user": return "Needs you"
        case "blocked": return "Blocked"
        case "idle": return "Idle"
        case "completed": return "Completed"
        case "working": return "Working"
        case "active": return "Active"
        default: return state.capitalized
        }
    }
}

// MARK: - Timeline items

private struct TimelineItemView: View {
    let item: TimelineItem
    let isExpanded: Bool
    let onToggle: () -> Void

    var body: some View {
        switch item {
        case .user(let event):
            UserBubble(event: event)
        case .assistant(let event):
            AssistantBubble(event: event)
        case .tool(let call, let result):
            ToolRow(call: call, result: result, isExpanded: isExpanded, onToggle: onToggle)
        case .orphanTool(let event):
            ToolRow(call: event, result: event, isExpanded: isExpanded, onToggle: onToggle, orphan: true)
        }
    }
}

private struct UserBubble: View {
    let event: SessionEvent

    var body: some View {
        HStack {
            Spacer(minLength: 40)
            Text(event.contentText ?? "")
                .font(.callout)
                .textSelection(.enabled)
                .padding(10)
                .background(Color.blue.opacity(0.15), in: RoundedRectangle(cornerRadius: 12))
                .frame(alignment: .trailing)
        }
    }
}

private struct AssistantBubble: View {
    let event: SessionEvent

    var body: some View {
        MarkdownText(event.contentText ?? "")
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 10))
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
    let onToggle: () -> Void
    var orphan: Bool = false

    private var summary: String { TimelineBuilder.inputSummary(for: call) }
    private var toolName: String { call.toolName ?? (orphan ? "Tool" : "Tool") }
    private var isPending: Bool { result == nil && !orphan }
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

    private var expandedIds: Set<String> = []
    private var pollTask: Task<Void, Never>?

    func isExpanded(_ id: String) -> Bool { expandedIds.contains(id) }

    func toggleExpanded(_ id: String) {
        if expandedIds.contains(id) { expandedIds.remove(id) } else { expandedIds.insert(id) }
        objectWillChange.send()
    }

    func start(sessionId: String, appState: AppState) async {
        if isInitialLoading {
            await reload(sessionId: sessionId, appState: appState)
        }
        startPollingIfActive(sessionId: sessionId, appState: appState)
    }

    func stop() {
        pollTask?.cancel()
        pollTask = nil
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
        } catch LonghouseAPIError.notAuthenticated {
            errorMessage = "Session expired."
        } catch {
            errorMessage = "Couldn't load session: \(error.localizedDescription)"
        }
        isInitialLoading = false
        startPollingIfActive(sessionId: sessionId, appState: appState)
    }

    func send(text: String, sessionId: String, appState: AppState) async -> Bool {
        guard let api = LonghouseAPI(host: appState.serverURL) else { return false }
        isSending = true
        defer { isSending = false }
        do {
            try await api.sendLive(id: sessionId, text: text)
            if let events = try? await api.sessionEvents(id: sessionId) {
                self.items = TimelineBuilder.build(events: events)
            }
            return true
        } catch {
            errorMessage = "Send failed: \(error.localizedDescription)"
            return false
        }
    }

    private func startPollingIfActive(sessionId: String, appState: AppState) {
        pollTask?.cancel()
        guard shouldPoll else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_500_000_000)
                if Task.isCancelled { break }
                await self?.pollTick(sessionId: sessionId, appState: appState)
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
        }
        if !shouldPoll {
            pollTask?.cancel()
            pollTask = nil
        }
    }

    private var shouldPoll: Bool {
        guard let detail else { return false }
        let active: Set<String> = ["running", "thinking", "working", "needs_user", "blocked", "active"]
        if let presence = detail.presenceState, active.contains(presence) { return true }
        if let status = detail.status, active.contains(status) { return true }
        return false
    }
}
