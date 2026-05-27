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

    @ViewBuilder
    private var content: some View {
        switch viewModel.state {
        case .initial:
            nonScrollingShell {
                ProgressView().controlSize(.large)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        case .empty:
            nonScrollingShell {
                emptyView
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        case .error(let message):
            nonScrollingShell {
                errorView(message)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        case .loaded(let sessions):
            timelineBody(sessions: sessions)
        }
    }

    /// Wrap non-scroll states so the connection strip still appears at the
    /// top of the screen even when there's no list to scroll. The loaded
    /// state renders the strip *inside* the ScrollView instead so it
    /// scrolls up with the large title.
    @ViewBuilder
    private func nonScrollingShell<Inner: View>(@ViewBuilder _ inner: () -> Inner) -> some View {
        VStack(spacing: 0) {
            ConnectionStatusStrip(state: effectiveConnectionState)
                .padding(.horizontal, 16)
                .padding(.top, 8)
            inner()
        }
    }

    var body: some View {
        NavigationStack(path: $path) {
            content
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
                viewModel.startStream(using: appState)
                consumePendingPushIfNeeded()
                Task {
                    await appState.ensurePushRegistrationIfPossible()
                }
            }
            .onAppear {
                WebTranscriptWebViewPool.prewarm()
                viewModel.resumeStream(using: appState)
            }
            .onDisappear {
                viewModel.stopStream()
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active {
                    Task {
                        await viewModel.refresh(using: appState, reloadWidget: true)
                        viewModel.startStream(using: appState)
                    }
                } else {
                    viewModel.stopStream()
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: .longhouseOpenSessionFromPush)) { note in
                if let sessionID = note.object as? String {
                    openSession(sessionID: sessionID)
                }
            }
        }
    }

    private func timelineBody(sessions: [SessionSummary]) -> some View {
        ScrollView {
            // Strip lives inside the scroll content so it scrolls up with
            // the large title and tucks under the compact nav bar instead
            // of pinning awkwardly under it like a stuck banner.
            LazyVStack(alignment: .leading, spacing: 14) {
                ConnectionStatusStrip(state: effectiveConnectionState)
                    .padding(.horizontal, 0)
                timelineSection(title: "Recent", sessions: sessions, emphasized: false)
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)
            .padding(.bottom, 18)
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
        let cardAccent = timelineCardAccentColor(session, connectionState: connectionState)

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
                .fill(cardAccent)
                .frame(width: emphasized ? 4 : 3)
                .padding(.vertical, 12)
        }
        .overlay {
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(cardAccent.opacity(emphasized ? 0.45 : 0.18), lineWidth: emphasized ? 1.2 : 0.8)
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
        let attentionTone = timelineAttentionTone(session.timelineStatusTone)
        let withinDeadline = session.runtimeDisplay.activityRecency == "live"
        let sessionStale = !withinDeadline && !isClosed
        // Pulse only when global is healthy AND the server's own
        // phase-signal deadline hasn't passed. Anything else freezes.
        let pulsing = globalHealthy && withinDeadline && attentionTone == .working
        let color = globalHealthy && !sessionStale ? timelineStatusColor(session) : .secondary
        let backgroundOpacity = globalHealthy && !sessionStale && attentionTone == .working ? 0.22 : 0.14

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
        .background(color.opacity(backgroundOpacity), in: Capsule())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(runtimeBadgeAccessibilityLabel(for: session, stale: sessionStale))
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

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var animate = false

    var body: some View {
        let shouldPulse = pulsing && !reduceMotion

        ZStack {
            if shouldPulse {
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
        .onAppear { animate = shouldPulse }
        .onChange(of: shouldPulse) { _, isPulsing in
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

/// Four-way state for the timeline screen. Replaces the prior cluster of
/// `isInitialLoading` / `errorMessage` / `recent.isEmpty` booleans, which
/// allowed nonsense combinations (loading + error + data) and forced the
/// view body to re-derive the state from if-else order.
enum TimelineLoadState: Equatable {
    case initial
    case empty
    case error(String)
    case loaded([SessionSummary])
}

@MainActor
final class TimelineViewModel: ObservableObject {
    @Published private(set) var state: TimelineLoadState = .initial
    @Published var lastUpdatedAt: Date?
    @Published private(set) var consecutiveRefreshFailures = 0

    private var streamTask: Task<Void, Never>?
    private var stream: TimelineSessionsStream?
    private var reconcileTask: Task<Void, Never>?
    private var persistTask: Task<Void, Never>?
    private var lastWidgetReloadAt: Date?
    private var isRefreshInFlight = false
    private var loggedFirstPaint = false
    private var streamGeneration: UInt64 = 0
    private var hasReceivedFirstConnect = false
    private let limit = 40
    private let reconcileIntervalNanoseconds: UInt64 = 120_000_000_000 // 120s safety net
    private let persistDebounceNanoseconds: UInt64 = 250_000_000 // 250ms cache/widget coalesce
    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "Timeline")

    var connectionState: ConnectionState {
        ConnectionState.derive(failures: consecutiveRefreshFailures, lastUpdatedAt: lastUpdatedAt)
    }

    private var isInitial: Bool {
        if case .initial = state { return true }
        return false
    }

    private var hasLoadedSessions: Bool {
        if case .loaded = state { return true }
        return false
    }

    func load(using appState: AppState) async {
        guard isInitial else { return }
        if let cached = TimelineCacheStore.load(serverURL: appState.serverURL) {
            applySessions(cached.sessions, source: "cache")
            lastUpdatedAt = cached.savedAt
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
            state = .error("Invalid server URL")
            return
        }
        let startedAt = Date()
        let generation = streamGeneration
        isRefreshInFlight = true
        defer { isRefreshInFlight = false }

        do {
            let sessions = try await api.recentSessions(limit: limit)
            // Drop stale snapshots from a previous stream lifetime — a slow
            // reconnect bootstrap mustn't overwrite newer stream-applied state.
            guard generation == streamGeneration || generation == 0 else {
                logger.info("timeline refresh dropped stale generation=\(generation, privacy: .public) current=\(self.streamGeneration, privacy: .public)")
                return
            }
            let attentionIds = Set(sessions.filter(\.needsAttention).map(\.id))
            applySessions(sessions, source: "network")
            schedulePersist(sessions: sessions, appState: appState)
            PushNotificationStore.removeResolvedAttentionNotifications(activeSessionIDs: attentionIds)
            self.lastUpdatedAt = Date()
            self.consecutiveRefreshFailures = 0
            if reloadWidget {
                reloadWidgetTimelineIfNeeded()
            }
            logger.info("timeline refresh finished sessions=\(sessions.count, privacy: .public) elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
        } catch LonghouseAPIError.notAuthenticated {
            consecutiveRefreshFailures += 1
            // Auth errors override stale data — re-login is required.
            state = .error("Session expired. Sign in again.")
            logger.error("timeline refresh unauthenticated elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
        } catch {
            consecutiveRefreshFailures += 1
            // While we have data on screen, refresh failures are silent —
            // the connection strip is the signal. Only surface an error
            // page when there's nothing else to show.
            if !hasLoadedSessions {
                state = .error("Couldn't load sessions: \(error.localizedDescription)")
            }
            logger.error("timeline refresh failed elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public) error=\(error.localizedDescription, privacy: .public)")
        }
    }

    func resumeStream(using appState: AppState) {
        startStream(using: appState)
        guard !isInitial else { return }
        Task { await refresh(using: appState, reloadWidget: true) }
    }

    func startStream(using appState: AppState) {
        guard streamTask == nil else { return }
        guard let baseURL = URL(string: appState.serverURL) else {
            logger.error("timeline stream invalid serverURL=\(appState.serverURL, privacy: .public)")
            return
        }
        streamGeneration &+= 1
        let generation = streamGeneration
        hasReceivedFirstConnect = false
        let stream = TimelineSessionsStream(baseURL: baseURL, limit: limit)
        self.stream = stream
        streamTask = Task { [weak self] in
            let events = await stream.start()
            for await event in events {
                guard let self else { break }
                await self.handleStreamEvent(event, generation: generation, appState: appState)
            }
            // Stream ended (cancellation or terminal 401). Clear the slot
            // so resumeStream / scenePhase can spin up a new task.
            await self?.streamLoopDidExit(generation: generation)
        }
        startReconcileSafetyNet(using: appState, generation: generation)
    }

    func stopStream() {
        // Bump generation first so any event already in flight is dropped
        // by the guard in handleStreamEvent before it can mutate state.
        streamGeneration &+= 1
        streamTask?.cancel()
        streamTask = nil
        if let stream {
            Task { await stream.stop() }
        }
        stream = nil
        reconcileTask?.cancel()
        reconcileTask = nil
        // Flush any pending debounced cache/widget save before tearing down
        // so a fast stream stop (scene background) doesn't drop the last
        // snapshot. The detached task in schedulePersist already snapshots
        // sessions by value, so flushing == waiting for it to finish.
        if let pending = persistTask {
            persistTask = nil
            Task { await pending.value }
        }
    }

    private func streamLoopDidExit(generation: UInt64) {
        guard generation == streamGeneration else { return }
        streamTask = nil
    }

    private func startReconcileSafetyNet(using appState: AppState, generation: UInt64) {
        reconcileTask?.cancel()
        let interval = reconcileIntervalNanoseconds
        reconcileTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: interval)
                if Task.isCancelled { break }
                guard let self else { break }
                if await self.streamGenerationMatches(generation) {
                    await self.refresh(using: appState, reloadWidget: true)
                } else {
                    break
                }
            }
        }
    }

    private func streamGenerationMatches(_ generation: UInt64) -> Bool {
        generation == streamGeneration
    }

    private func handleStreamEvent(
        _ event: TimelineSessionsStream.Event,
        generation: UInt64,
        appState: AppState
    ) async {
        guard generation == streamGeneration else { return }
        switch event {
        case .connected:
            consecutiveRefreshFailures = 0
            // Reconnects need a snapshot resync because the stream has no
            // Last-Event-ID replay. The very first connect is already
            // covered by the `load()` REST bootstrap, so skip it. Don't
            // stamp lastUpdatedAt yet on reconnects — wait until the
            // bootstrap actually lands so connectionState doesn't lie.
            if hasReceivedFirstConnect {
                logger.info("timeline stream reconnected — bootstrapping snapshot")
                await refresh(using: appState, reloadWidget: true)
            } else {
                hasReceivedFirstConnect = true
                lastUpdatedAt = Date()
            }
        case .upsert(let card, _, _):
            applyUpsert(card.sessionSummary, appState: appState)
            lastUpdatedAt = Date()
            consecutiveRefreshFailures = 0
        case .remove(let threadId, _, _):
            applyRemove(threadId: threadId, appState: appState)
            lastUpdatedAt = Date()
            consecutiveRefreshFailures = 0
        case .heartbeat:
            lastUpdatedAt = Date()
            consecutiveRefreshFailures = 0
        case .disconnected(let error):
            consecutiveRefreshFailures += 1
            if let apiError = error as? LonghouseAPIError, case .notAuthenticated = apiError {
                state = .error("Session expired. Sign in again.")
            }
            logger.info("timeline stream disconnected error=\(error?.localizedDescription ?? "nil", privacy: .public)")
        }
    }

    private func applyUpsert(_ session: SessionSummary, appState: AppState) {
        var current = currentSessions()
        let incomingThread = session.threadId
        // Match either by thread (when both sides have one) or by head id —
        // pre-stream cached rows can have threadId == nil and would otherwise
        // duplicate or fail to delete until the next REST bootstrap. Also
        // sweep on head id always, so a legacy row without threadId still
        // gets replaced when a stream upsert with a threadId arrives for it.
        current.removeAll { existing in
            if existing.id == session.id { return true }
            if let incomingThread, let existingThread = existing.threadId {
                return existingThread == incomingThread
            }
            return false
        }
        current.append(session)
        current.sort { lhs, rhs in
            anchorDate(for: lhs) > anchorDate(for: rhs)
        }
        if current.count > limit {
            current = Array(current.prefix(limit))
        }
        applySessions(current, source: "stream")
        schedulePersist(sessions: current, appState: appState)
        reloadWidgetTimelineIfNeeded()
    }

    private func applyRemove(threadId: String, appState: AppState) {
        var current = currentSessions()
        let before = current.count
        // Match by threadId when present, otherwise fall back to head id —
        // legacy cached rows without a threadId still need to be reachable.
        current.removeAll { existing in
            if let existingThread = existing.threadId {
                return existingThread == threadId
            }
            return existing.id == threadId
        }
        guard current.count != before else { return }
        applySessions(current, source: "stream")
        schedulePersist(sessions: current, appState: appState)
        reloadWidgetTimelineIfNeeded()
    }

    private func currentSessions() -> [SessionSummary] {
        if case .loaded(let sessions) = state { return sessions }
        return []
    }

    /// Coalesce cache + widget-snapshot disk writes. Stream upsert/remove
    /// bursts (5–20/sec during an active session) would otherwise hit the
    /// main actor with synchronous JSON-encode + file writes per event.
    private func schedulePersist(sessions: [SessionSummary], appState: AppState) {
        persistTask?.cancel()
        let serverURL = appState.serverURL
        let delay = persistDebounceNanoseconds
        persistTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: delay)
            if Task.isCancelled { return }
            await Task.detached(priority: .utility) {
                TimelineCacheStore.save(sessions: sessions, serverURL: serverURL)
                WidgetSessionSnapshotStore.save(sessions: sessions)
            }.value
            _ = self
        }
    }

    private func anchorDate(for session: SessionSummary) -> Date {
        if let anchor = session.timelineAnchor, let date = LonghouseDateParser.parse(anchor) {
            return date
        }
        return .distantPast
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
        state = sessions.isEmpty ? .empty : .loaded(sessions)
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

private enum TimelineAttentionTone {
    case working
    case attention
    case quiet
    case closed
}

private func timelineAttentionTone(_ tone: String) -> TimelineAttentionTone {
    switch tone.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
    case "thinking", "running":
        return .working
    case "blocked", "stalled":
        return .attention
    case "closed":
        return .closed
    default:
        return .quiet
    }
}

private func timelineCardAccentColor(_ session: SessionSummary, connectionState: ConnectionState) -> Color {
    // A non-healthy connection suppresses per-card attention color; the
    // connection strip owns that severity signal.
    guard connectionState == .healthy else { return .secondary.opacity(0.45) }

    switch timelineAttentionTone(session.timelineBorderTone) {
    case .attention:
        return .orange
    case .working:
        return .primary
    case .closed:
        return .secondary.opacity(0.38)
    case .quiet:
        return .secondary.opacity(0.45)
    }
}

private func timelineStatusColor(_ session: SessionSummary) -> Color {
    switch timelineAttentionTone(session.timelineStatusTone) {
    case .working:
        return .primary
    case .attention:
        return .orange
    case .closed:
        return .secondary.opacity(0.7)
    case .quiet:
        return .secondary
    }
}

private func managementColor(_ session: SessionSummary) -> Color {
    .secondary
}

private func providerColor(_: String?) -> Color {
    .secondary
}

private func providerIcon(_ provider: String?) -> String {
    switch provider?.lowercased() {
    case "codex": return "terminal"
    case "antigravity": return "sparkles"
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

/// "How long in current state" — the headline number in the pill.
/// Uses `timelineAnchor`, which the backend re-anchors on phase changes
/// and progress signals (server/zerg/services/session_runtime.py).
/// Returns nil for closed sessions (we don't want to show a counter there).
func stateDurationLabel(for session: SessionSummary) -> String? {
    if session.timelineStatusLabel == "Closed" { return nil }
    guard let date = parseLonghouseDate(session.timelineAnchor) else { return nil }
    return compactDuration(since: date)
}

private func runtimeBadgeAccessibilityLabel(for session: SessionSummary, stale: Bool) -> String {
    var parts = [session.timelineStatusLabel]
    if let duration = stateDurationLabel(for: session) {
        parts.append(duration)
    }
    if stale {
        parts.append("stale")
    }
    return parts.joined(separator: ", ")
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
