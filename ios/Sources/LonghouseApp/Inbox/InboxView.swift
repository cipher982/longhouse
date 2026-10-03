import OSLog
import SwiftUI
import WidgetKit

/// Filter the rows already on screen.
///
/// This is the keystroke path: no network, no debounce, no state transition, so
/// it runs inside the view body and costs one frame. It is metadata-only by
/// design — the phone holds no transcript, which is exactly why the server lane
/// exists, and the escalation row is what asks for it.
///
/// Every whitespace-separated token must match somewhere, so a two-word query
/// narrows rather than widens, and token order does not matter.
func filterTimelineSessions(_ sessions: [SessionSummary], query: String) -> [SessionSummary] {
    let tokens = query.split(whereSeparator: \.isWhitespace).map(String.init)
    guard !tokens.isEmpty else { return sessions }
    return sessions.filter { session in
        let haystack = session.searchHaystack
        return tokens.allSatisfy { token in
            haystack.range(of: token, options: [.caseInsensitive, .diacriticInsensitive]) != nil
        }
    }
}

extension SessionSummary {
    /// Everything the timeline filter matches against.
    ///
    /// This is the whole local corpus: the fields a timeline card can show plus
    /// the server's match snippet when a search produced the row. It
    /// deliberately excludes anything the user cannot see, so a row is never
    /// matched by text the card does not explain.
    var searchHaystack: String {
        [
            title,
            summaryTitle,
            summary,
            firstUserMessage,
            project,
            provider,
            gitBranch,
            matchSnippet,
            timelineMachineLabel,
        ]
        .compactMap { $0 }
        .joined(separator: "\n")
    }
}

enum TimelineRowRole: Equatable {
    case needsYou
    case newResult
    case open
    case recent
}

struct TimelineInboxLayout: Equatable {
    let needsYou: [SessionSummary]
    let newResults: [SessionSummary]
    let open: [SessionSummary]
    let recent: [SessionSummary]
}

/// Order one timeline section by a key that does not move while you watch it.
///
/// Never `timelineAnchorAt`: that is an evidence clock, and the engine re-stamps
/// a session's heads continuously — including idle Helm sessions — so ordering
/// on it made rows swap places every few seconds ("popcorn"). Mirrors the web's
/// `startedAtMs`/`historySortKey`: open work lays out by launch, history by the
/// last time the session actually did something. Equal keys keep input order.
func timelineDisplayOrder(_ sessions: [SessionSummary]) -> [SessionSummary] {
    sessions
        .enumerated()
        .map { (time: timelineDisplayTime(for: $0.element), index: $0.offset, session: $0.element) }
        .sorted { $0.time != $1.time ? $0.time > $1.time : $0.index < $1.index }
        .map(\.session)
}

private func timelineDisplayTime(for session: SessionSummary) -> Date {
    let raw = session.isClosed ? (session.lastActivityAt ?? session.startedAt) : session.startedAt
    guard let raw, let date = LonghouseDateParser.parse(raw) else { return .distantPast }
    return date
}

/// Obligation-ranked presentation over canonical server facts. This does not
/// create another state model: `working_set`, explicit interaction facts, and
/// `unread` remain authoritative.
func buildTimelineInboxLayout(_ sessions: [SessionSummary]) -> TimelineInboxLayout {
    var needsYou: [SessionSummary] = []
    var newResults: [SessionSummary] = []
    var open: [SessionSummary] = []
    var recent: [SessionSummary] = []

    for session in sessions {
        if session.isOpen {
            if session.needsAttention {
                needsYou.append(session)
            } else {
                open.append(session)
            }
        } else if session.stateFacts.unread {
            newResults.append(session)
        } else {
            recent.append(session)
        }
    }

    // Keys first, as in applyUpsert: parsing inside the comparator re-parses
    // each row's date O(log n) times. Equal dates keep server order.
    newResults = newResults
        .enumerated()
        .map { (date: resultDate(for: $0.element), index: $0.offset, session: $0.element) }
        .sorted { $0.date != $1.date ? $0.date > $1.date : $0.index < $1.index }
        .map(\.session)
    return TimelineInboxLayout(
        needsYou: timelineDisplayOrder(needsYou),
        newResults: newResults,
        open: timelineDisplayOrder(open),
        recent: timelineDisplayOrder(recent)
    )
}

private func resultDate(for session: SessionSummary) -> Date {
    guard let value = session.stateFacts.lastResultAt else {
        return .distantPast
    }
    return LonghouseDateParser.parse(value) ?? .distantPast
}

@MainActor
struct TimelineView: View {
    @EnvironmentObject var appState: AppState
    @Environment(\.scenePhase) private var scenePhase
    private let initialDeviceId: String?
    @StateObject private var viewModel: TimelineViewModel
    @State private var launchSheetPresented = false
    @State private var settingsPresented = false
    @State private var path: [SessionRoute] = []
    @State private var isShowingBugReport = false
    @State private var bugReportAutoStartFix = false
    @State private var isShowingBugReportSavedAlert = false
    @State private var bugReportSavedPending = false
    @State private var bugReportSessionToOpen: String?
    @State private var bugReportScreenshot: Data?
    @State private var bugReportContextJSON = Data("{}".utf8)
    init(initialDeviceId: String? = nil) {
        self.initialDeviceId = initialDeviceId
        _viewModel = StateObject(wrappedValue: TimelineViewModel(deviceId: initialDeviceId))
    }
    @State private var searchText = ""

    private var effectiveConnectionBanner: TimelineConnectivityBanner {
        viewModel.connectionBanner
    }

    private var normalizedSearch: String {
        searchText.trimmingCharacters(in: .whitespacesAndNewlines)
    }


    @ViewBuilder
    private var content: some View {
        if normalizedSearch.isEmpty {
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
        } else {
            searchBody
        }
    }

    /// The timeline, filtered in place. Typing never replaces these rows with a
    /// spinner: the resident sessions are the local corpus, so the filter is
    /// synchronous and the server lane can only ever add a section below them.
    private var searchBody: some View {
        TimelineSessionList(
            sessions: filteredSessions,
            connectivityBanner: effectiveConnectionBanner,
            search: searchPresentation
        )
    }

    /// The rows the phone already holds. This is the whole local search corpus.
    private var residentSessions: [SessionSummary] {
        if case .loaded(let sessions) = viewModel.state { return sessions }
        return []
    }

    private var filteredSessions: [SessionSummary] {
        filterTimelineSessions(residentSessions, query: normalizedSearch)
    }

    private var searchPresentation: TimelineSearchPresentation {
        TimelineSearchPresentation(
            query: normalizedSearch,
            visibleCount: filteredSessions.count,
            residentCount: residentSessions.count,
            remote: viewModel.searchState,
            remoteLane: viewModel.searchLane,
            onSearchAll: {
                viewModel.searchRemote(query: normalizedSearch, lane: .lexical, using: appState)
            },
            onSearchByMeaning: {
                viewModel.searchRemote(query: normalizedSearch, lane: .semantic, using: appState)
            },
            onRetry: { viewModel.retrySearch(using: appState) }
        )
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
            .background { EmberHearthBackground() }
            .navigationTitle("Timeline")
            .searchable(text: $searchText, prompt: "Filter sessions")
            .navigationDestination(for: SessionRoute.self) { route in
                SessionView(
                    sessionId: route.sessionId,
                    fallbackTitle: route.fallbackTitle,
                    fallbackSubtitle: route.fallbackSubtitle,
                    onTranscriptDiagnostics: nil,
                    onOpenSubagent: { childSessionId in
                        // A worker pushes onto the same stack: it is part of this
                        // session's work, so Back returns to the row that spawned it.
                        path.append(SessionRoute(sessionId: childSessionId, fallbackTitle: "Subagent"))
                    },
                    onOpenSession: { newSessionId in
                        path.append(SessionRoute(sessionId: newSessionId, fallbackTitle: "Bug report"))
                    }
                )
            }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        presentBugReport()
                    } label: {
                        Label("Report a problem", systemImage: "exclamationmark.bubble")
                            .labelStyle(.iconOnly)
                    }
                    .accessibilityHint("Capture diagnostics without opening a session")
                    // See SessionView's overflow menu: standalone glass buttons
                    // need a concrete style so the gold tint resolves correctly.
                    .foregroundStyle(Color.primary)
                    .accessibilityIdentifier("timeline-report-problem")
                    .transaction { transaction in
                        transaction.animation = nil
                    }
                    .opacity(path.isEmpty ? 1 : 0)
                    .disabled(!path.isEmpty)
                    .accessibilityHidden(!path.isEmpty)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        launchSheetPresented = true
                    } label: {
                        Image(systemName: "plus")
                            .fontWeight(.semibold)
                            .foregroundStyle(Ember.gold)
                            .accessibilityLabel("Start session")
                    }
                    .emberProminentToolbarButton()
                    // Keep the parent toolbar slot stable during a push, but
                    // remove its controls immediately once the session route
                    // owns the navigation bar. Otherwise UIKit cross-fades
                    // the timeline actions over the destination controls.
                    .transaction { transaction in
                        transaction.animation = nil
                    }
                    .opacity(path.isEmpty ? 1 : 0)
                    .disabled(!path.isEmpty)
                    .accessibilityHidden(!path.isEmpty)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        settingsPresented = true
                    } label: {
                        Image(systemName: "gearshape")
                            .accessibilityLabel("Settings")
                    }
                    .foregroundStyle(Color.primary)
                    .transaction { transaction in
                        transaction.animation = nil
                    }
                    .opacity(path.isEmpty ? 1 : 0)
                    .disabled(!path.isEmpty)
                    .accessibilityHidden(!path.isEmpty)
                }
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
            .sheet(isPresented: $isShowingBugReport, onDismiss: finishBugReportDismissal) {
                BugReportSheet(
                    sourceSessionID: nil,
                    contextJSON: bugReportContextJSON,
                    screenshotData: bugReportScreenshot,
                    autoStartFix: bugReportAutoStartFix,
                    onSent: { sessionID in
                        bugReportSavedPending = false
                        bugReportSessionToOpen = sessionID
                        isShowingBugReport = false
                    },
                    onSaved: {
                        bugReportSessionToOpen = nil
                        bugReportSavedPending = true
                        isShowingBugReport = false
                    }
                )
            }
            .overlay(alignment: .bottom) {
                if isShowingBugReportSavedAlert {
                    BugReportSavedBanner(
                        onStartFix: {
                            isShowingBugReportSavedAlert = false
                            bugReportSavedPending = false
                            bugReportAutoStartFix = true
                            isShowingBugReport = true
                        },
                        onDone: {
                            isShowingBugReportSavedAlert = false
                        }
                    )
                    .padding(.horizontal, 16)
                    .padding(.bottom, 16)
                    .safeAreaPadding(.bottom, 8)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
            .refreshable {
                if normalizedSearch.isEmpty {
                    await viewModel.refresh(using: appState, reloadWidget: true, force: true)
                } else {
                    viewModel.searchRemote(
                        query: normalizedSearch,
                        lane: viewModel.searchLane ?? .lexical,
                        using: appState
                    )
                    await viewModel.awaitRemoteSearch()
                }
            }
            .task(id: normalizedSearch) {
                // Typing is local. The rows are filtered in the body, so the
                // only thing a keystroke has to do here is abandon a server
                // answer that no longer describes what is on screen.
                viewModel.cancelRemoteSearch()
            }
            .task {
                await viewModel.load(using: appState)
                // No prewarm here: WebView creation is ~0.5 s of main thread
                // and ran before the cached timeline's first frame, ahead of
                // the stream and a tapped push. The idle task below warms it.
                if scenePhase == .active {
                    viewModel.startStream(using: appState)
                }
                consumePendingPushIfNeeded()
                Task {
                    await appState.ensurePushRegistrationIfPossible()
                }
            }
            // Warm WebKit while the timeline is idle, not from the session
            // route where it would compete with the first detail request.
            .task {
                try? await Task.sleep(nanoseconds: 500_000_000)
                guard !Task.isCancelled else { return }
                WebTranscriptWebViewPool.prewarm()
            }
            .onDisappear {
                viewModel.stopStream()
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active {
                    Task {
                        await viewModel.refresh(using: appState, reloadWidget: true)
                        viewModel.startStream(using: appState)
                        if !normalizedSearch.isEmpty, viewModel.searchLane != nil {
                            viewModel.retrySearch(using: appState)
                        }
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
    private func finishBugReportDismissal() {
        if let sessionID = bugReportSessionToOpen {
            bugReportSessionToOpen = nil
            path.append(SessionRoute(sessionId: sessionID, fallbackTitle: "Bug report"))
        } else if bugReportSavedPending {
            bugReportSavedPending = false
            isShowingBugReportSavedAlert = true
        }
    }
    private func presentBugReport() {
        guard path.isEmpty else { return }
        bugReportAutoStartFix = false
        bugReportSavedPending = false
        bugReportSessionToOpen = nil
        bugReportScreenshot = nil
        bugReportContextJSON = BugReportContext.timeline(serverURL: appState.serverURL)
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 350_000_000)
            bugReportScreenshot = BugReportScreenCapture.captureJPEG()
            isShowingBugReport = true
        }
    }


    private func timelineBody(sessions: [SessionSummary]) -> some View {
        TimelineSessionList(sessions: sessions, connectivityBanner: effectiveConnectionBanner)
    }

    private var emptyView: some View {
        TimelineEmptyView(scopedToMachine: initialDeviceId != nil)
    }

    private func errorView(_ message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 36))
                .foregroundStyle(Ember.ember)
            Text(message)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
            Button("Try again") {
                Task { await viewModel.refresh(using: appState, reloadWidget: true, force: true) }
            }
            .buttonStyle(.bordered)
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
