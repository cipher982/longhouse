import SwiftUI
import WidgetKit

@MainActor
struct TimelineView: View {
    @EnvironmentObject var appState: AppState
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var viewModel = TimelineViewModel()
    @State private var path: [SessionRoute] = []

    var body: some View {
        NavigationStack(path: $path) {
            Group {
                if viewModel.isInitialLoading {
                    ProgressView().controlSize(.large)
                } else if let error = viewModel.errorMessage, viewModel.isEmpty {
                    errorView(error)
                } else if viewModel.isEmpty {
                    emptyView
                } else {
                    timelineBody
                }
            }
            .navigationTitle("Timeline")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    if viewModel.isRefreshing && !viewModel.isInitialLoading {
                        ProgressView().controlSize(.small)
                    }
                }
            }
            .refreshable { await viewModel.refresh(using: appState, reloadWidget: true) }
            .task {
                await appState.ensurePushRegistrationIfPossible()
                await viewModel.load(using: appState)
                viewModel.startAutoRefresh(using: appState)
                consumePendingPushIfNeeded()
            }
            .onDisappear {
                viewModel.stopAutoRefresh()
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active {
                    Task {
                        await viewModel.refresh(using: appState, reloadWidget: true)
                        viewModel.startAutoRefresh(using: appState)
                    }
                } else {
                    viewModel.stopAutoRefresh()
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: .longhouseOpenSessionFromPush)) { note in
                if let sessionID = note.object as? String {
                    openSession(sessionID: sessionID)
                }
            }
        }
    }

    private var timelineBody: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 20) {
                if !viewModel.attention.isEmpty {
                    timelineSection(title: "Needs you", sessions: viewModel.attention, emphasized: true)
                }
                if !viewModel.recent.isEmpty {
                    timelineSection(title: "Recent", sessions: viewModel.recent, emphasized: false)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 18)
        }
        .background(Color(.systemGroupedBackground))
        .navigationDestination(for: SessionRoute.self) { route in
            SessionView(sessionId: route.sessionId, fallbackTitle: route.fallbackTitle)
        }
    }

    private func timelineSection(title: String, sessions: [SessionSummary], emphasized: Bool) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title)
                .font(.headline.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 2)

            VStack(spacing: 10) {
                ForEach(sessions) { session in
                    NavigationLink(value: SessionRoute(sessionId: session.id, fallbackTitle: session.title)) {
                        TimelineSessionCardRow(session: session, emphasized: emphasized)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private var emptyView: some View {
        ContentUnavailableView(
            "No active sessions",
            systemImage: "rectangle.stack",
            description: Text("Recent active sessions will appear here as Longhouse syncs them.")
        )
    }

    private func errorView(_ message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 36))
                .foregroundStyle(.orange)
            Text(message)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
            Button("Try again") {
                Task { await viewModel.refresh(using: appState, reloadWidget: true) }
            }
            .buttonStyle(.borderedProminent)
        }
        .padding()
    }

    private func consumePendingPushIfNeeded() {
        if let sessionID = PushNotificationStore.consumePendingSessionID(), !sessionID.isEmpty {
            openSession(sessionID: sessionID)
        }
    }

    private func openSession(sessionID: String) {
        path = [SessionRoute(sessionId: sessionID, fallbackTitle: "Session")]
    }
}

private struct SessionRoute: Hashable {
    let sessionId: String
    let fallbackTitle: String
}

private struct TimelineSessionCardRow: View {
    let session: SessionSummary
    let emphasized: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text(session.projectLabel)
                    .font(.headline.weight(.semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Spacer(minLength: 12)
                Text(relativeTime(session.timelineAnchor))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    ProviderBadge(session: session)
                    if let branch = nonEmpty(session.gitBranch) {
                        MetadataBadge(systemImage: "arrow.triangle.branch", text: branch)
                    }
                    if let origin = nonEmpty(session.headOriginLabel), origin != nonEmpty(session.homeLabel) {
                        MetadataBadge(text: "Head: \(origin)")
                    }
                }
                .lineLimit(1)

                HStack(spacing: 8) {
                    RuntimeBadge(session: session)
                    MetadataBadge(text: session.managementLabel)
                }
            }

            VStack(alignment: .leading, spacing: 6) {
                Text(session.title)
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)

                if let summary = session.summaryPreview {
                    Text(summary)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text("Generating summary")
                        .font(.subheadline)
                        .foregroundStyle(.tertiary)
                }
            }

            Divider()

            HStack(spacing: 6) {
                Text("\(session.turnCount) \(session.turnCount == 1 ? "turn" : "turns")")
                    .foregroundStyle(turnColor(session.turnCount))
                Text("·")
                    .foregroundStyle(.tertiary)
                Text("\(session.toolCount) \(session.toolCount == 1 ? "tool" : "tools")")
                Spacer(minLength: 12)
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.tertiary)
            }
            .font(.caption.weight(.medium))
            .foregroundStyle(.secondary)
        }
        .padding(14)
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(alignment: .leading) {
            RoundedRectangle(cornerRadius: 2)
                .fill(runtimeColor(session))
                .frame(width: emphasized ? 4 : 3)
                .padding(.vertical, 12)
        }
        .overlay {
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(runtimeColor(session).opacity(emphasized ? 0.45 : 0.18), lineWidth: emphasized ? 1.2 : 0.8)
        }
    }
}

private struct ProviderBadge: View {
    let session: SessionSummary

    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: providerIcon(session.provider))
                .font(.caption2.weight(.semibold))
            Text(session.providerLabel)
                .font(.caption.weight(.semibold))
        }
        .foregroundStyle(providerColor(session.provider))
    }
}

private struct RuntimeBadge: View {
    let session: SessionSummary

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(runtimeColor(session))
                .frame(width: 7, height: 7)
            Text(session.displayPhaseLabel)
                .font(.caption.weight(.semibold))
                .lineLimit(1)
        }
        .foregroundStyle(runtimeColor(session))
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(runtimeColor(session).opacity(0.14), in: Capsule())
    }
}

private struct MetadataBadge: View {
    let systemImage: String?
    let text: String

    init(systemImage: String? = nil, text: String) {
        self.systemImage = systemImage
        self.text = text
    }

    var body: some View {
        HStack(spacing: 4) {
            if let systemImage {
                Image(systemName: systemImage)
                    .font(.caption2.weight(.semibold))
            }
            Text(text)
                .font(.caption.weight(.medium))
        }
        .foregroundStyle(.secondary)
        .padding(.horizontal, 9)
        .padding(.vertical, 5)
        .background(Color(.tertiarySystemGroupedBackground), in: Capsule())
    }
}

@MainActor
final class TimelineViewModel: ObservableObject {
    @Published var attention: [SessionSummary] = []
    @Published var recent: [SessionSummary] = []
    @Published var errorMessage: String?
    @Published var isInitialLoading = true
    @Published var isRefreshing = false
    @Published var lastUpdatedAt: Date?

    private var refreshTask: Task<Void, Never>?
    private var lastWidgetReloadAt: Date?

    var isEmpty: Bool { attention.isEmpty && recent.isEmpty }

    func load(using appState: AppState) async {
        if !isInitialLoading { return }
        await refresh(using: appState, reloadWidget: true)
    }

    func refresh(using appState: AppState, reloadWidget: Bool = false) async {
        guard !isRefreshing else { return }
        guard let api = LonghouseAPI(host: appState.serverURL) else {
            errorMessage = "Invalid server URL"
            isInitialLoading = false
            return
        }
        isRefreshing = true
        defer {
            isRefreshing = false
            isInitialLoading = false
        }

        do {
            let sessions = try await api.recentActiveSessions(limit: 40)
            let attention = sessions.filter(\.needsAttention)
            let attentionIds = Set(attention.map(\.id))
            self.attention = attention
            self.recent = sessions.filter { !attentionIds.contains($0.id) }
            self.lastUpdatedAt = Date()
            self.errorMessage = nil
            if reloadWidget {
                reloadWidgetTimelineIfNeeded()
            }
        } catch LonghouseAPIError.notAuthenticated {
            errorMessage = "Session expired. Sign in again."
        } catch {
            errorMessage = "Couldn't load sessions: \(error.localizedDescription)"
        }
    }

    func startAutoRefresh(using appState: AppState) {
        guard refreshTask == nil else { return }
        refreshTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                if Task.isCancelled { break }
                await self?.refresh(using: appState, reloadWidget: true)
            }
        }
    }

    func stopAutoRefresh() {
        refreshTask?.cancel()
        refreshTask = nil
    }

    private func reloadWidgetTimelineIfNeeded() {
        let now = Date()
        guard lastWidgetReloadAt == nil || now.timeIntervalSince(lastWidgetReloadAt!) > 60 else {
            return
        }
        WidgetCenter.shared.reloadAllTimelines()
        lastWidgetReloadAt = now
    }
}

private func nonEmpty(_ value: String?) -> String? {
    guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty else {
        return nil
    }
    return trimmed
}

private func runtimeColor(_ session: SessionSummary) -> Color {
    if session.isBlocked { return .orange }
    if session.isNeedsUser { return .yellow }
    if session.presenceState == "running" { return .green }
    if session.presenceState == "thinking" { return .orange }
    if session.isExecuting { return .orange }
    if session.isIdle || session.status == "completed" { return .secondary }
    return .blue
}

private func providerColor(_ provider: String?) -> Color {
    switch provider {
    case "codex": return .green
    case "gemini": return .blue
    case "claude": return .orange
    case "zai": return .purple
    default: return .secondary
    }
}

private func providerIcon(_ provider: String?) -> String {
    switch provider {
    case "codex": return "terminal"
    case "gemini": return "sparkles"
    case "claude": return "sparkle"
    default: return "chevron.left.forwardslash.chevron.right"
    }
}

private func turnColor(_ turnCount: Int) -> Color {
    if turnCount >= 50 { return .red }
    if turnCount >= 20 { return .orange }
    return .secondary
}

private func relativeTime(_ value: String?) -> String {
    guard let date = parseLonghouseDate(value) else { return "Recent" }
    let formatter = RelativeDateTimeFormatter()
    formatter.unitsStyle = .abbreviated
    return formatter.localizedString(for: date, relativeTo: Date())
}

private func parseLonghouseDate(_ value: String?) -> Date? {
    guard let value else { return nil }
    let fractional = ISO8601DateFormatter()
    fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = fractional.date(from: value) {
        return date
    }
    return ISO8601DateFormatter().date(from: value)
}
