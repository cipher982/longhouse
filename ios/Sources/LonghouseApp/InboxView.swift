import OSLog
import SwiftUI
import WidgetKit

@MainActor
struct TimelineView: View {
    @EnvironmentObject var appState: AppState
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var viewModel = TimelineViewModel()
    @State private var path: [SessionRoute] = []
    @State private var launchSheetPresented = false
    #if DEBUG
    @State private var forcedConnectionState: ConnectionState?
    #endif

    private var effectiveConnectionState: ConnectionState {
        #if DEBUG
        forcedConnectionState ?? viewModel.connectionState
        #else
        viewModel.connectionState
        #endif
    }

    var body: some View {
        NavigationStack(path: $path) {
            VStack(spacing: 0) {
                // Render the strip above all content branches so empty,
                // error, loading, and timeline states all share the same
                // connection signal. timelineBody no longer renders its
                // own copy.
                ConnectionStatusStrip(state: effectiveConnectionState)
                    .padding(.horizontal, 16)
                    .padding(.top, 8)
                Group {
                    if viewModel.isInitialLoading {
                        ProgressView().controlSize(.large)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    } else if let error = viewModel.errorMessage, viewModel.isEmpty {
                        errorView(error)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    } else if viewModel.isEmpty {
                        emptyView
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    } else {
                        timelineBody
                    }
                }
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Timeline")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        launchSheetPresented = true
                    } label: {
                        Image(systemName: "plus.circle.fill")
                            .accessibilityLabel("Start session")
                    }
                }
                #if DEBUG
                ToolbarItem(placement: .topBarLeading) {
                    Menu {
                        Button("Auto (\(label(for: viewModel.connectionState)))") {
                            forcedConnectionState = nil
                        }
                        Divider()
                        ForEach([ConnectionState.connecting, .healthy, .reconnecting, .offline], id: \.self) { s in
                            Button("Force: \(label(for: s))") { forcedConnectionState = s }
                        }
                    } label: {
                        Image(systemName: "ladybug")
                            .accessibilityLabel("Debug: force connection state")
                    }
                }
                #endif
            }
            .sheet(isPresented: $launchSheetPresented) {
                LaunchSessionSheet { sessionId in
                    launchSheetPresented = false
                    path.append(SessionRoute(sessionId: sessionId, fallbackTitle: "New session"))
                }
            }
            .refreshable { await viewModel.refresh(using: appState, reloadWidget: true, force: true) }
            .task {
                WebTranscriptWebViewPool.prewarm()
                await viewModel.load(using: appState)
                viewModel.startAutoRefresh(using: appState)
                consumePendingPushIfNeeded()
                Task {
                    await appState.ensurePushRegistrationIfPossible()
                }
            }
            .onAppear {
                WebTranscriptWebViewPool.prewarm()
                viewModel.resumeAutoRefresh(using: appState)
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
                if !viewModel.recent.isEmpty {
                    timelineSection(title: "Recent", sessions: viewModel.recent, emphasized: false)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 18)
        }
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
                        TimelineSessionCardRow(
                            session: session,
                            emphasized: emphasized,
                            connectionState: viewModel.connectionState
                        )
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private var emptyView: some View {
        ContentUnavailableView(
            "No timeline sessions",
            systemImage: "rectangle.stack",
            description: Text("Sessions will appear here as Longhouse syncs them.")
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
                Task { await viewModel.refresh(using: appState, reloadWidget: true, force: true) }
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
        PushNotificationStore.clearPendingSessionID(sessionID)
        path = [SessionRoute(sessionId: sessionID, fallbackTitle: "Session")]
    }
}

private struct SessionRoute: Hashable {
    let sessionId: String
    let fallbackTitle: String
}

struct TimelineSessionCardRow: View {
    let session: SessionSummary
    let emphasized: Bool
    var connectionState: ConnectionState = .healthy

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text(session.projectLabel)
                    .font(.headline.weight(.semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Spacer(minLength: 12)
            }

            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    ProviderBadge(session: session)
                    if let branch = session.timelineBranchBadgeLabel {
                        MetadataBadge(systemImage: "arrow.triangle.branch", text: branch)
                    }
                }
                .lineLimit(1)

                HStack(spacing: 8) {
                    RuntimeBadge(session: session, connectionState: connectionState)
                    CapabilityBadge(session: session)
                }
            }

            VStack(alignment: .leading, spacing: 6) {
                Text(session.title)
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)

                if let summary = session.timelineSummaryPreview {
                    Text(summary)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
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
    let connectionState: ConnectionState

    var body: some View {
        let isClosed = session.timelineStatusLabel == "Closed"
        // Only .healthy preserves the status color. .connecting,
        // .reconnecting, and .offline all retract to .secondary so a
        // non-pulsing colored dot can't masquerade as "live".
        let globalHealthy = connectionState == .healthy
        let withinDeadline = phaseSignalFresh(session)
        let sessionStale = !withinDeadline && !isClosed
        // Pulse only when global is healthy AND the server's own
        // phase-signal deadline hasn't passed. Anything else freezes.
        let pulsing = globalHealthy && withinDeadline && !isClosed
        let color = globalHealthy && !sessionStale ? timelineStatusColor(session) : .secondary

        HStack(spacing: 6) {
            LivenessDot(color: color, pulsing: pulsing)
            Text(session.timelineStatusLabel)
                .font(.caption.weight(.semibold))
                .lineLimit(1)
            if let duration = stateDurationLabel(for: session) {
                Text("·")
                    .foregroundStyle(.secondary)
                Text(duration)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .monospacedDigit()
            }
            if sessionStale {
                Text("·")
                    .foregroundStyle(.tertiary)
                Text("stale")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.orange)
                    .lineLimit(1)
            }
        }
        .foregroundStyle(color)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(color.opacity(0.14), in: Capsule())
    }
}

/// Slim status strip pinned below the nav bar via safeAreaInset.
/// Healthy = invisible (the absence of a strip is the signal).
/// Anything else paints a thin colored bar with text. Pull to refresh
/// is the retry path; this view is purely informational.
struct ConnectionStatusStrip: View {
    let state: ConnectionState

    var body: some View {
        if let style = style(for: state) {
            HStack(spacing: 6) {
                if let symbol = style.symbol {
                    Image(systemName: symbol)
                        .font(.caption2.weight(.semibold))
                }
                Text(style.label)
                    .font(.caption.weight(.semibold))
                Spacer(minLength: 0)
            }
            .foregroundStyle(style.foreground)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(style.background, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .accessibilityLabel(style.label)
        }
    }

    private struct Style {
        let label: String
        let symbol: String?
        let foreground: Color
        let background: Color
    }

    private func style(for state: ConnectionState) -> Style? {
        switch state {
        case .healthy:
            #if DEBUG
            // Always render in DEBUG so the codepath is visible — we
            // can't tell "healthy" apart from "the strip never ran" by
            // looking at it. Release builds hide healthy entirely.
            return Style(label: "Connected", symbol: nil,
                         foreground: .green,
                         background: Color.green.opacity(0.14))
            #else
            return nil
            #endif
        case .connecting:
            return Style(label: "Connecting", symbol: nil,
                         foreground: .secondary,
                         background: Color(.tertiarySystemGroupedBackground))
        case .reconnecting:
            return Style(label: "Reconnecting", symbol: "arrow.triangle.2.circlepath",
                         foreground: .yellow,
                         background: Color.yellow.opacity(0.18))
        case .offline:
            return Style(label: "Offline", symbol: "exclamationmark.triangle.fill",
                         foreground: .red,
                         background: Color.red.opacity(0.18))
        }
    }
}

#if DEBUG
private func label(for state: ConnectionState) -> String {
    switch state {
    case .connecting: return "Connecting"
    case .healthy:    return "Healthy"
    case .reconnecting: return "Reconnecting"
    case .offline:    return "Offline"
    }
}
#endif

private struct LivenessDot: View {
    let color: Color
    let pulsing: Bool

    @State private var animate = false

    var body: some View {
        ZStack {
            if pulsing {
                Circle()
                    .stroke(color, lineWidth: 1.4)
                    .scaleEffect(animate ? 2.0 : 1.0)
                    .opacity(animate ? 0.0 : 0.55)
                    .frame(width: 8, height: 8)
                    .animation(.easeOut(duration: 1.2).repeatForever(autoreverses: false), value: animate)
            }
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)
        }
        .frame(width: 12, height: 12)
        // Drive `animate` from the `pulsing` prop directly so LazyVStack
        // recycling (which can swap pulsing on without firing onAppear)
        // still kicks the animation back on.
        .onAppear { animate = pulsing }
        .onChange(of: pulsing) { _, isPulsing in
            animate = isPulsing
        }
    }
}

private struct CapabilityBadge: View {
    let session: SessionSummary

    var body: some View {
        Text(session.managementLabel)
            .font(.caption.weight(.semibold))
            .lineLimit(1)
            .foregroundStyle(managementColor(session))
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(managementColor(session).opacity(0.14), in: Capsule())
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

/// Connection state, derived from the auto-refresh contract — no time
/// thresholds. The poll either succeeded, is in-flight, or failed; we
/// just read what already happened.
enum ConnectionState: Equatable, Hashable {
    case connecting        // No successful refresh yet (cold start in flight)
    case healthy           // Most recent scheduled refresh succeeded
    case reconnecting      // 1 consecutive failure; retry already scheduled
    case offline           // 2+ consecutive failures

    /// Pure derivation of connection state from observable inputs. Lives at
    /// file scope so unit tests can lock in the state machine without having
    /// to spin up a `TimelineViewModel` (which depends on `AppState`/network).
    static func derive(failures: Int, lastUpdatedAt: Date?) -> ConnectionState {
        // Defensive: a negative failure count is semantically "no failures".
        let failures = max(0, failures)
        // Cold start (no successful poll yet): stay in .connecting through
        // the first failure so a single hiccup doesn't immediately read as
        // "reconnecting" — there's nothing to reconnect to. Two failures
        // with no success on record means we're truly offline.
        if lastUpdatedAt == nil {
            return failures >= 2 ? .offline : .connecting
        }
        // We have at least one successful poll on record: standard ladder.
        switch failures {
        case 0:  return .healthy
        case 1:  return .reconnecting
        default: return .offline
        }
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
    @Published private(set) var consecutiveRefreshFailures = 0

    private var autoRefreshTask: Task<Void, Never>?
    private var lastWidgetReloadAt: Date?
    private var activeRefreshCount = 0
    private var isRefreshInFlight = false
    private var loggedFirstPaint = false
    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "Timeline")

    var connectionState: ConnectionState {
        ConnectionState.derive(failures: consecutiveRefreshFailures, lastUpdatedAt: lastUpdatedAt)
    }

    var isEmpty: Bool { attention.isEmpty && recent.isEmpty }

    func load(using appState: AppState) async {
        if !isInitialLoading { return }
        if let cached = TimelineCacheStore.load(serverURL: appState.serverURL) {
            applySessions(cached.sessions, source: "cache")
            lastUpdatedAt = cached.savedAt
            isInitialLoading = false
            logger.info("timeline cache hit sessions=\(cached.sessions.count, privacy: .public)")
            Task { [weak self] in
                await self?.refresh(using: appState, reloadWidget: true)
            }
            return
        }
        logger.info("timeline cache miss")
        await refresh(using: appState, reloadWidget: true)
    }

    func refresh(using appState: AppState, reloadWidget: Bool = false, force: Bool = false) async {
        if isRefreshInFlight && !force {
            logger.debug("timeline refresh skipped reason=in_flight")
            return
        }
        guard let api = LonghouseAPI(host: appState.serverURL) else {
            errorMessage = "Invalid server URL"
            isInitialLoading = false
            return
        }
        let startedAt = Date()
        isRefreshInFlight = true
        activeRefreshCount += 1
        isRefreshing = true
        defer {
            activeRefreshCount = max(0, activeRefreshCount - 1)
            isRefreshing = activeRefreshCount > 0
            isInitialLoading = false
            isRefreshInFlight = false
        }

        do {
            let sessions = try await api.recentSessions(limit: 40)
            let attentionIds = Set(sessions.filter(\.needsAttention).map(\.id))
            applySessions(sessions, source: "network")
            TimelineCacheStore.save(sessions: sessions, serverURL: appState.serverURL)
            WidgetSessionSnapshotStore.save(sessions: sessions)
            PushNotificationStore.removeResolvedAttentionNotifications(activeSessionIDs: attentionIds)
            self.lastUpdatedAt = Date()
            self.errorMessage = nil
            self.consecutiveRefreshFailures = 0
            if reloadWidget {
                reloadWidgetTimelineIfNeeded()
            }
            logger.info("timeline refresh finished sessions=\(sessions.count, privacy: .public) elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
        } catch LonghouseAPIError.notAuthenticated {
            errorMessage = "Session expired. Sign in again."
            consecutiveRefreshFailures += 1
            logger.error("timeline refresh unauthenticated elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
        } catch {
            errorMessage = "Couldn't load sessions: \(error.localizedDescription)"
            consecutiveRefreshFailures += 1
            logger.error("timeline refresh failed elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public) error=\(error.localizedDescription, privacy: .public)")
        }
    }

    func resumeAutoRefresh(using appState: AppState) {
        startAutoRefresh(using: appState)
        guard !isInitialLoading else { return }
        Task { await refresh(using: appState, reloadWidget: true) }
    }

    func startAutoRefresh(using appState: AppState) {
        guard autoRefreshTask == nil else { return }
        autoRefreshTask = Task { [weak self] in
            while !Task.isCancelled {
                let delay = self?.autoRefreshDelayNanoseconds ?? 4_000_000_000
                try? await Task.sleep(nanoseconds: delay)
                if Task.isCancelled { break }
                await self?.refresh(using: appState, reloadWidget: true)
            }
        }
    }

    func stopAutoRefresh() {
        autoRefreshTask?.cancel()
        autoRefreshTask = nil
    }

    private var autoRefreshDelayNanoseconds: UInt64 {
        switch consecutiveRefreshFailures {
        case 0:
            return 4_000_000_000
        case 1:
            return 8_000_000_000
        default:
            return 16_000_000_000
        }
    }

    private func reloadWidgetTimelineIfNeeded() {
        let now = Date()
        guard lastWidgetReloadAt == nil || now.timeIntervalSince(lastWidgetReloadAt!) > 60 else {
            return
        }
        WidgetCenter.shared.reloadAllTimelines()
        lastWidgetReloadAt = now
    }

    private func applySessions(_ sessions: [SessionSummary], source: String) {
        let attention = sessions.filter(\.needsAttention)
        self.attention = attention
        self.recent = sessions
        if !loggedFirstPaint {
            loggedFirstPaint = true
            logger.info("timeline first paint source=\(source, privacy: .public) sessions=\(sessions.count, privacy: .public)")
        }
    }
}

private func nonEmpty(_ value: String?) -> String? {
    guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty else {
        return nil
    }
    return trimmed
}

private func runtimeColor(_ session: SessionSummary) -> Color {
    switch session.timelineBorderTone {
    case "active": return .blue
    case "running": return .green
    case "thinking", "blocked", "stalled": return .orange
    case "closed": return .secondary
    default: return .secondary
    }
}

private func timelineStatusColor(_ session: SessionSummary) -> Color {
    switch session.timelineStatusTone {
    case "active": return .blue
    case "running": return .green
    case "thinking", "blocked", "stalled": return .orange
    case "closed": return .secondary
    default: return .secondary
    }
}

private func managementColor(_ session: SessionSummary) -> Color {
    .secondary
}

private func providerColor(_ provider: String?) -> Color {
    switch provider?.lowercased() {
    case "codex": return .green
    case "gemini": return .blue
    case "claude": return .orange
    case "zai": return .purple
    default: return .secondary
    }
}

private func providerIcon(_ provider: String?) -> String {
    switch provider?.lowercased() {
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
    return LonghouseDateParser.parse(value)
}

// MARK: - Liveness + duration helpers (RuntimeBadge)

/// Per-session deadline check: require the server's `phase.expiresAt`.
/// Without a server-stamped deadline we refuse to claim freshness — a
/// missing/malformed payload should freeze the dot, not pulse forever.
func phaseSignalFresh(_ session: SessionSummary) -> Bool {
    guard let raw = session.runtimeFacts?.phase.expiresAt,
          let expires = parseLonghouseDate(raw) else {
        return false
    }
    return Date() < expires
}

/// "How long in current state" — the headline number in the pill.
/// Uses `timelineAnchor`, which the backend re-anchors on phase changes
/// and progress signals (server/zerg/services/session_runtime.py).
/// Returns nil for closed sessions (we don't want to show a counter there).
func stateDurationLabel(for session: SessionSummary) -> String? {
    if session.timelineStatusLabel == "Closed" { return nil }
    guard let date = parseLonghouseDate(session.timelineAnchor) else { return nil }
    return compactDuration(since: date)
}

/// Compact, no-"ago" duration: "5s", "12s", "3m", "1h", "2d".
func compactDuration(since date: Date) -> String {
    let interval = max(0, Date().timeIntervalSince(date))
    let seconds = Int(interval)
    if seconds < 60 { return "\(seconds)s" }
    let minutes = seconds / 60
    if minutes < 60 { return "\(minutes)m" }
    let hours = minutes / 60
    if hours < 24 { return "\(hours)h" }
    let days = hours / 24
    return "\(days)d"
}
