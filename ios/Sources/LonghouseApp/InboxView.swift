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
    @State private var settingsPresented = false
    #if DEBUG
    @State private var forcedConnectionBanner: TimelineConnectivityBanner?
    #endif

    private var effectiveConnectionBanner: TimelineConnectivityBanner {
        #if DEBUG
        forcedConnectionBanner ?? viewModel.connectionBanner
        #else
        viewModel.connectionBanner
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
            ConnectionStatusStrip(banner: effectiveConnectionBanner)
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
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        settingsPresented = true
                    } label: {
                        Image(systemName: "gearshape")
                            .accessibilityLabel("Settings")
                    }
                }
                #if DEBUG
                ToolbarItem(placement: .topBarLeading) {
                    Menu {
                        Button("Auto (\(label(for: viewModel.connectionBanner)))") {
                            forcedConnectionBanner = nil
                        }
                        Divider()
                        ForEach([
                            TimelineConnectivityBanner.none,
                            .updating,
                            .degraded,
                            .offline,
                            .authRequired,
                        ], id: \.self) { banner in
                            Button("Force: \(label(for: banner))") { forcedConnectionBanner = banner }
                        }
                    } label: {
                        Image(systemName: "ladybug")
                            .accessibilityLabel("Debug: force connection state")
                    }
                }
                #endif
            }
            .sheet(isPresented: $settingsPresented) {
                SettingsView()
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
                consumePendingPushIfNeeded()
            }
            .onDisappear {
                viewModel.stopStream()
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active {
                    Task {
                        await viewModel.refresh(using: appState, reloadWidget: true)
                        viewModel.startStream(using: appState)
                        consumePendingPushIfNeeded()
                    }
                } else {
                    viewModel.stopStream()
                }
            }
            // Push payloads are posted from nonisolated APNs delegate code;
            // NotificationCenter delivers on the posting thread, so hop to main
            // before mutating SwiftUI state.
            .onReceive(NotificationCenter.default.publisher(for: .longhouseOpenSessionFromPush).receive(on: DispatchQueue.main)) { note in
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
                ConnectionStatusStrip(banner: effectiveConnectionBanner)
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
                            connectivityBanner: viewModel.connectionBanner
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
        let trimmed = sessionID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        PushNotificationStore.clearPendingSessionID(trimmed)
        path = [SessionRoute(sessionId: trimmed, fallbackTitle: "Session")]
    }
}

private struct SessionRoute: Hashable {
    let sessionId: String
    let fallbackTitle: String
}

struct TimelineSessionCardRow: View {
    let session: SessionSummary
    let emphasized: Bool
    var connectivityBanner: TimelineConnectivityBanner = .none

    var body: some View {
        let signal = TimelineSignal.resolve(for: session, suppressed: connectivityBanner != .none)
        let cardAccent = signal.accentColor

        // Three-line row built for glanceability:
        //  - kicker: project · branch ........................ when
        //  - headline: ● <frozen server-resolved title>
        //  - status: demoted runtime state, colored by signal
        // The frozen `title` (server timeline_title) is the muscle-memory anchor;
        // the leading dot + status carry "is it active / waiting on me / done".
        // No Managed badge, no turns/tools — that was the dead right half.
        HStack(alignment: .top, spacing: 11) {
            ProviderGlyph(provider: session.provider, size: 30)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(session.projectLabel)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    if let branch = session.timelineBranchBadgeLabel {
                        Text(branch)
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                            .layoutPriority(-1)
                    }
                    Spacer(minLength: 6)
                    if let duration = stateDurationLabel(for: session) {
                        Text(duration)
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(.tertiary)
                            .monospacedDigit()
                    }
                }

                HStack(alignment: .firstTextBaseline, spacing: 7) {
                    LivenessDot(color: signal.dotColor, pulsing: signal.pulses)
                        .alignmentGuide(.firstTextBaseline) { d in d[VerticalAlignment.center] + 4 }
                    Text(session.title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                }
                // The dot is color-only; fold its meaning into the headline so
                // VoiceOver announces "Waiting on you" / "Working" rather than
                // leaving amber as the sole, invisible-to-VoiceOver code.
                .accessibilityElement(children: .combine)
                .accessibilityLabel("\(session.title), \(signal.accessibilityState)")

                CompactRuntimeLine(session: session, signal: signal)

                // B-lite drift line: the live, drifting summary title parked on a
                // demoted, low-contrast line where movement is legitimate. The
                // frozen headline above stays put (muscle memory); this is the
                // "what is it doing now" channel, shown only while actively
                // working so it never churns under a resting row.
                if signal == .working, let drift = session.driftTitle {
                    Text("now: \(drift)")
                        .font(.caption2)
                        .italic()
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }
        }
        .padding(.vertical, 11)
        .padding(.horizontal, 12)
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(alignment: .leading) {
            RoundedRectangle(cornerRadius: 2)
                .fill(cardAccent)
                .frame(width: emphasized ? 4 : 3)
                .padding(.vertical, 10)
        }
        .overlay {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(cardAccent.opacity(emphasized ? 0.45 : 0.16), lineWidth: emphasized ? 1.2 : 0.8)
        }
    }
}

/// Demoted runtime status line under the headline: the state label, colored by
/// the row signal, with an inline "stale" flag. The dot moved up to the
/// headline, so this line is text-only and subordinate.
private struct CompactRuntimeLine: View {
    let session: SessionSummary
    let signal: TimelineSignal

    var body: some View {
        let sessionStale = signal == .quiet && session.shouldAnnotateTimelineStatusAsStale

        HStack(spacing: 5) {
            Text(session.timelineStatusLabel)
                .font(.caption.weight(.medium))
                .foregroundStyle(signal.statusColor)
                .lineLimit(1)
            if sessionStale {
                Text("· stale")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.orange)
                    .lineLimit(1)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(runtimeBadgeAccessibilityLabel(for: session, stale: sessionStale))
    }
}

/// Slim status strip pinned below the nav bar via safeAreaInset.
/// Healthy = invisible (the absence of a strip is the signal).
/// Anything else paints a thin colored bar with text. Pull to refresh
/// is the retry path; this view is purely informational.
struct ConnectionStatusStrip: View {
    let banner: TimelineConnectivityBanner

    var body: some View {
        if let style = style(for: banner) {
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

    private func style(for banner: TimelineConnectivityBanner) -> Style? {
        switch banner {
        case .none:
            return nil
        case .updating:
            return Style(label: "Updating", symbol: "arrow.triangle.2.circlepath",
                         foreground: .yellow,
                         background: Color.yellow.opacity(0.18))
        case .degraded:
            return Style(label: "Connection degraded", symbol: "exclamationmark.triangle",
                         foreground: .orange,
                         background: Color.orange.opacity(0.18))
        case .offline:
            return Style(label: "Offline", symbol: "exclamationmark.triangle.fill",
                         foreground: .red,
                         background: Color.red.opacity(0.18))
        case .authRequired:
            return Style(label: "Sign in required", symbol: "person.crop.circle.badge.exclamationmark",
                         foreground: .red,
                         background: Color.red.opacity(0.18))
        }
    }
}

#if DEBUG
private func label(for banner: TimelineConnectivityBanner) -> String {
    switch banner {
    case .none: return "Hidden"
    case .updating: return "Updating"
    case .degraded: return "Degraded"
    case .offline: return "Offline"
    case .authRequired: return "Sign in required"
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



protocol TimelineSessionsClient: Sendable {
    func recentSessions(limit: Int) async throws -> [SessionSummary]
}

extension LonghouseAPI: TimelineSessionsClient {}

struct TimelineSessionsStreamSource: Sendable {
    let start: @Sendable () async -> AsyncStream<TimelineSessionsStream.Event>
    let stop: @Sendable () async -> Void

    static func live(baseURL: URL, limit: Int) -> TimelineSessionsStreamSource {
        let stream = TimelineSessionsStream(baseURL: baseURL, limit: limit)
        return TimelineSessionsStreamSource(
            start: { await stream.start() },
            stop: { await stream.stop() }
        )
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
    @Published private(set) var connectivity = TimelineConnectivityState()
    @Published private(set) var connectivityNow = Date()

    private var streamTask: Task<Void, Never>?
    private var stream: TimelineSessionsStreamSource?
    private var reconcileTask: Task<Void, Never>?
    private var persistTask: Task<Void, Never>?
    private var connectivityClockTask: Task<Void, Never>?
    private var lastWidgetReloadAt: Date?
    private var isRefreshInFlight = false
    private var loggedFirstPaint = false
    private var streamGeneration: UInt64 = 0
    private var hasReceivedFirstConnect = false
    private var streamAuthRefreshAttempted = false
    private let apiFactory: (String) -> TimelineSessionsClient?
    private let streamFactory: (URL, Int) -> TimelineSessionsStreamSource
    private let enableRealtime: Bool
    private let enableConnectivityClock: Bool
    private let limit = 40
    private let reconcileIntervalNanoseconds: UInt64 = 120_000_000_000 // 120s safety net
    private let connectivityClockIntervalNanoseconds: UInt64 = 15_000_000_000 // 15s freshness tick
    private let persistDebounceNanoseconds: UInt64 = 250_000_000 // 250ms cache/widget coalesce
    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "Timeline")

    var connectionBanner: TimelineConnectivityBanner {
        connectivity.banner(at: connectivityNow)
    }

    init(
        apiFactory: @escaping (String) -> TimelineSessionsClient? = { LonghouseAPI(host: $0) },
        streamFactory: @escaping (URL, Int) -> TimelineSessionsStreamSource = { baseURL, limit in
            TimelineSessionsStreamSource.live(baseURL: baseURL, limit: limit)
        },
        enableRealtime: Bool = true,
        enableConnectivityClock: Bool = true
    ) {
        self.apiFactory = apiFactory
        self.streamFactory = streamFactory
        self.enableRealtime = enableRealtime
        self.enableConnectivityClock = enableConnectivityClock
    }

    func connectionBanner(at now: Date) -> TimelineConnectivityBanner {
        connectivity.banner(at: now)
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
        startConnectivityClock()
        guard isInitial else { return }
        if let cached = TimelineCacheStore.load(serverURL: appState.serverURL) {
            applySessions(cached.sessions, source: "cache")
            applyConnectivity(.cacheLoaded(hasLoadedData: !cached.sessions.isEmpty, savedAt: cached.savedAt))
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
        guard let api = apiFactory(appState.serverURL) else {
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
            applyConnectivity(.snapshotSucceeded(hasLoadedData: !sessions.isEmpty))
            schedulePersist(sessions: sessions, appState: appState)
            PushNotificationStore.removeResolvedAttentionNotifications(activeSessionIDs: attentionIds)
            if reloadWidget {
                reloadWidgetTimelineIfNeeded()
            }
            logger.info("timeline refresh finished sessions=\(sessions.count, privacy: .public) elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
        } catch LonghouseAPIError.notAuthenticated {
            guard generation == streamGeneration || generation == 0 else {
                logger.info("timeline refresh auth failure dropped stale generation=\(generation, privacy: .public) current=\(self.streamGeneration, privacy: .public)")
                return
            }
            applyConnectivity(.authFailed)
            appState.handleExpiredSession()
            logger.error("timeline refresh unauthenticated elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
        } catch {
            guard generation == streamGeneration || generation == 0 else {
                logger.info("timeline refresh failure dropped stale generation=\(generation, privacy: .public) current=\(self.streamGeneration, privacy: .public)")
                return
            }
            applyConnectivity(.snapshotFailed)
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
        guard enableRealtime else { return }
        startConnectivityClock()
        guard streamTask == nil else { return }
        guard let baseURL = URL(string: appState.serverURL) else {
            logger.error("timeline stream invalid serverURL=\(appState.serverURL, privacy: .public)")
            return
        }
        streamGeneration &+= 1
        let generation = streamGeneration
        hasReceivedFirstConnect = false
        let stream = streamFactory(baseURL, limit)
        self.stream = stream
        streamTask = Task { [weak self] in
            let events = await stream.start()
            for await event in events {
                guard let self else { break }
                await self.handleStreamEvent(event, generation: generation, appState: appState)
            }
            // Stream ended (cancellation or terminal 401). Clear the slot
            // so resumeStream / scenePhase can spin up a new task.
            self?.streamLoopDidExit(generation: generation)
        }
        startReconcileSafetyNet(using: appState, generation: generation)
    }

    func stopStream() {
        // Bump generation first so any event already in flight is dropped
        // by the guard in handleStreamEvent before it can mutate state.
        streamGeneration &+= 1
        applyConnectivity(.lifecycleStopped)
        streamTask?.cancel()
        streamTask = nil
        if let stream {
            Task { await stream.stop() }
        }
        stream = nil
        reconcileTask?.cancel()
        reconcileTask = nil
        stopConnectivityClock()
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
                if self.streamGenerationMatches(generation) {
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
            // Reconnects need a snapshot resync because the stream has no
            // Last-Event-ID replay. The very first connect is already
            // covered by the `load()` REST bootstrap, so skip it. Don't
            // stamp data freshness on reconnects — wait until the bootstrap
            // actually lands so the banner doesn't lie.
            streamAuthRefreshAttempted = false
            if hasReceivedFirstConnect {
                applyConnectivity(.streamSignal(.reconnected), generation: generation)
                logger.info("timeline stream reconnected — bootstrapping snapshot")
                await refresh(using: appState, reloadWidget: true)
            } else {
                hasReceivedFirstConnect = true
                applyConnectivity(.streamSignal(.firstConnected), generation: generation)
            }
        case .upsert(let card, _, _):
            applyUpsert(card.sessionSummary, appState: appState)
            applyConnectivity(.streamSignal(.upsert), generation: generation)
        case .remove(let threadId, _, _):
            applyRemove(threadId: threadId, appState: appState)
            applyConnectivity(.streamSignal(.remove), generation: generation)
        case .heartbeat:
            applyConnectivity(.streamSignal(.heartbeat), generation: generation)
        case .disconnected(let error):
            let reason = classifyStreamDisconnect(error)
            if reason == .authFailure {
                await handleStreamAuthFailure(generation: generation, appState: appState)
            } else {
                applyConnectivity(.streamDisconnected(reason), generation: generation)
            }
            logger.info("timeline stream disconnected reason=\(String(describing: reason), privacy: .public) error=\(error?.localizedDescription ?? "nil", privacy: .public)")
        }
    }

    private func handleStreamAuthFailure(generation: UInt64, appState: AppState) async {
        guard generation == streamGeneration else { return }
        guard !streamAuthRefreshAttempted else {
            applyConnectivity(.streamDisconnected(.authFailure), generation: generation)
            return
        }
        streamAuthRefreshAttempted = true

        await refresh(using: appState, reloadWidget: true, force: true)
        guard generation == streamGeneration,
              appState.isAuthenticated,
              connectivity.reachability == .reachable else { return }

        streamTask = nil
        stream = nil
        startStream(using: appState)
    }

    private func applyConnectivity(
        _ event: TimelineConnectivityEvent,
        now: Date = Date(),
        generation: UInt64? = nil
    ) {
        var next = connectivity
        if let generation {
            next.apply(event, now: now, eventGeneration: generation, currentGeneration: streamGeneration)
        } else {
            next.apply(event, now: now)
        }
        connectivity = next
        connectivityNow = now
    }

    private func startConnectivityClock() {
        guard enableConnectivityClock, connectivityClockTask == nil else { return }
        let interval = connectivityClockIntervalNanoseconds
        connectivityClockTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: interval)
                if Task.isCancelled { break }
                self?.tickConnectivityClock()
            }
        }
    }

    private func stopConnectivityClock() {
        connectivityClockTask?.cancel()
        connectivityClockTask = nil
    }

    private func tickConnectivityClock() {
        connectivityNow = Date()
    }

    private func classifyStreamDisconnect(_ error: Error?) -> StreamDisconnectReason {
        guard let error else { return .serverEOF }
        if error is CancellationError { return .cancelled }
        if let apiError = error as? LonghouseAPIError, case .notAuthenticated = apiError {
            return .authFailure
        }
        if let urlError = error as? URLError {
            switch urlError.code {
            case .cancelled:
                return .cancelled
            case .notConnectedToInternet, .networkConnectionLost, .cannotFindHost,
                 .cannotConnectToHost, .dnsLookupFailed, .timedOut:
                return .networkError
            default:
                return .unknown
            }
        }
        return .unknown
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

// TimelineSignal lives in shared session models so the app card and the
// home-screen widget share one definition. Use TimelineSignal.resolve.

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
    // Use the lifecycle flag, the same "closed" source the signal uses, so the
    // dot/accent and the duration never disagree about whether a row is closed.
    if session.isClosed { return nil }
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
