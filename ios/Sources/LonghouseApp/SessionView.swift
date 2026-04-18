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
        .task(id: sessionId) { await viewModel.load(sessionId: sessionId, appState: appState) }
        .refreshable { await viewModel.reload(sessionId: sessionId, appState: appState) }
    }

    private var transcript: some View {
        Group {
            if viewModel.isInitialLoading {
                ProgressView().controlSize(.large)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let error = viewModel.errorMessage, viewModel.events.isEmpty {
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
            } else if viewModel.events.isEmpty {
                ContentUnavailableView(
                    "No messages yet",
                    systemImage: "bubble.left.and.bubble.right"
                )
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 12) {
                            if let detail = viewModel.detail {
                                SessionHeader(detail: detail)
                            }
                            ForEach(viewModel.events) { event in
                                EventBubble(event: event).id(event.id)
                            }
                        }
                        .padding(.horizontal)
                        .padding(.vertical, 12)
                    }
                    .onChange(of: viewModel.events.count) { _, _ in
                        if let last = viewModel.events.last {
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
            if detail.capabilities.liveControlAvailable {
                composerField(enabled: true)
            } else if detail.capabilities.replyToLiveSessionAvailable {
                composerField(enabled: true)
            } else {
                unmanagedFooter(detail: detail)
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

    private func unmanagedFooter(detail: SessionDetail) -> some View {
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
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
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

private struct EventBubble: View {
    let event: SessionEvent

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(roleLabel)
                .font(.caption.weight(.semibold))
                .foregroundStyle(roleColor)
            if let text = event.contentText, !text.isEmpty {
                Text(text)
                    .font(.callout)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else if let tool = event.toolName {
                VStack(alignment: .leading, spacing: 2) {
                    Text(tool).font(.caption.weight(.medium)).foregroundStyle(.secondary)
                    if let output = event.toolOutputText, !output.isEmpty {
                        Text(output.prefix(400) + (output.count > 400 ? "…" : ""))
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(8)
                    }
                }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(background, in: RoundedRectangle(cornerRadius: 10))
    }

    private var roleLabel: String {
        switch event.role {
        case "user": return "You"
        case "assistant": return "Assistant"
        case "tool": return event.toolName ?? "Tool"
        case "system": return "System"
        default: return event.role.capitalized
        }
    }

    private var roleColor: Color {
        switch event.role {
        case "user": return .blue
        case "assistant": return .primary
        case "tool": return .purple
        default: return .secondary
        }
    }

    private var background: Color {
        switch event.role {
        case "user": return Color.blue.opacity(0.10)
        case "tool": return Color.purple.opacity(0.08)
        default: return Color(.secondarySystemBackground)
        }
    }
}

@MainActor
final class SessionViewModel: ObservableObject {
    @Published var detail: SessionDetail?
    @Published var events: [SessionEvent] = []
    @Published var errorMessage: String?
    @Published var isInitialLoading = true
    @Published var isSending = false

    func load(sessionId: String, appState: AppState) async {
        if !isInitialLoading { return }
        await reload(sessionId: sessionId, appState: appState)
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
            self.events = events
            self.errorMessage = nil
        } catch LonghouseAPIError.notAuthenticated {
            errorMessage = "Session expired."
        } catch {
            errorMessage = "Couldn't load session: \(error.localizedDescription)"
        }
        isInitialLoading = false
    }

    func send(text: String, sessionId: String, appState: AppState) async -> Bool {
        guard let api = LonghouseAPI(host: appState.serverURL) else { return false }
        isSending = true
        defer { isSending = false }
        do {
            try await api.sendLive(id: sessionId, text: text)
            // Re-pull events after send so the user sees their message land.
            async let eventsTask = api.sessionEvents(id: sessionId)
            self.events = (try? await eventsTask) ?? self.events
            return true
        } catch {
            errorMessage = "Send failed: \(error.localizedDescription)"
            return false
        }
    }
}
