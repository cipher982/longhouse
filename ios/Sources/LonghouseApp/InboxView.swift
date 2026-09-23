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
        needsYou: needsYou,
        newResults: newResults,
        open: open,
        recent: recent
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
    @StateObject private var viewModel = TimelineViewModel()
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
                WebTranscriptWebViewPool.prewarm()
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

struct TimelineSessionList: View {
    let sessions: [SessionSummary]
    let connectivityBanner: TimelineConnectivityBanner
    /// Present only while the user is filtering. Its presence is what makes
    /// this a search view; the list itself, its order, and its sections do not
    /// change, because the resident rows are the filter's corpus.
    var search: TimelineSearchPresentation?

    var body: some View {
        // Once per render: as a computed property it was rebuilt, and its
        // new-results section re-sorted, for each of the four sections.
        let layout = buildTimelineInboxLayout(sessions)
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 14) {
                ConnectionStatusStrip(banner: connectivityBanner)

                if let search {
                    searchCountLine(search)
                }

                section(title: "Needs you", sessions: layout.needsYou, role: .needsYou)
                section(title: "New results", sessions: layout.newResults, role: .newResult)
                section(title: "Open", sessions: layout.open, role: .open)
                section(title: "Recent", sessions: layout.recent, role: .recent)

                if let search {
                    searchFooter(search)
                }
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)
            .padding(.bottom, 18)
        }
    }

    /// What the filter currently shows, stated before the rows rather than
    /// instead of them. Never reports absence while the server lane is still
    /// working: "0 of 28" and "nothing to match against" are different answers.
    private func searchCountLine(_ search: TimelineSearchPresentation) -> some View {
        Text(
            search.residentCount == 0
                ? "No sessions loaded yet"
                : "\(search.visibleCount) of \(search.residentCount) sessions"
        )
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
        .textCase(.uppercase)
        .padding(.horizontal, 2)
        .accessibilityIdentifier("timeline-search-count")
    }

    @ViewBuilder
    private func searchFooter(_ search: TimelineSearchPresentation) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Divider()
                .padding(.top, 2)

            switch search.remote {
            case .idle:
                searchActionRow(
                    title: "Search all sessions",
                    detail: "Last \(timelineSearchScopeDays) days, including sessions not loaded here",
                    systemImage: "magnifyingglass",
                    identifier: "timeline-search-all",
                    action: search.onSearchAll
                )
            case .loading:
                HStack(spacing: 9) {
                    ProgressView().controlSize(.small)
                    Text("Searching all sessions for “\(search.query)”…")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 8)
                .accessibilityIdentifier("timeline-search-in-flight")
            case .error(let message):
                VStack(alignment: .leading, spacing: 8) {
                    Text(message)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    Button("Try again", action: search.onRetry)
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                }
                .padding(.vertical, 4)
            case .empty:
                VStack(alignment: .leading, spacing: 10) {
                    Text("No session in the last \(timelineSearchScopeDays) days matches “\(search.query)”.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    if search.remoteLane == .lexical {
                        meaningSearchRow(search)
                    } else {
                        Button("Try again", action: search.onRetry)
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                    }
                }
                .padding(.vertical, 4)
            case .loaded(let sessions):
                searchResultsSection(sessions: sessions, search: search)
            }
        }
    }

    @ViewBuilder
    private func searchResultsSection(
        sessions: [SessionSummary],
        search: TimelineSearchPresentation
    ) -> some View {
        HStack {
            Text(search.remoteLane == .semantic ? "By meaning" : "From all sessions")
                .font(.headline.weight(.semibold))
                .accessibilityAddTraits(.isHeader)
            Spacer(minLength: 8)
            Text("\(sessions.count)")
                .font(.caption.weight(.semibold))
                .monospacedDigit()
        }
        .foregroundStyle(.secondary)
        .padding(.horizontal, 2)
        .padding(.top, 2)

        ForEach(sessions) { session in
            NavigationLink(value: SessionRoute(
                sessionId: session.id,
                fallbackTitle: session.title,
                fallbackSubtitle: session.identitySubtitle
            )) {
                TimelineSearchResultRow(session: session, query: search.query)
            }
            .buttonStyle(.plain)
        }

        // Offered after a keyword answer, not only after an empty one. Meaning
        // search earns its cost on the paraphrase the keyword lane ranked
        // weakly, which is exactly the case an empty-result trigger misses.
        if search.remoteLane == .lexical {
            meaningSearchRow(search)
        }
    }

    @ViewBuilder
    private func meaningSearchRow(_ search: TimelineSearchPresentation) -> some View {
        searchActionRow(
            title: "Search by meaning instead",
            detail: "Slower. Finds sessions that never used these words.",
            systemImage: "sparkle.magnifyingglass",
            identifier: "timeline-search-by-meaning",
            action: search.onSearchByMeaning
        )
    }

    private func searchActionRow(
        title: String,
        detail: String,
        systemImage: String,
        identifier: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: 11) {
                Image(systemName: systemImage)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .frame(width: 22)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .multilineTextAlignment(.leading)
                }
                Spacer(minLength: 6)
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.tertiary)
            }
            .padding(.vertical, 11)
            .padding(.horizontal, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Ember.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .stroke(Ember.hairline, lineWidth: 0.8)
            }
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier(identifier)
    }

    @ViewBuilder
    private func section(title: String, sessions: [SessionSummary], role: TimelineRowRole) -> some View {
        if !sessions.isEmpty {
            EmberSectionHeader(title: title, count: sessions.count)
                .padding(.top, role == .needsYou ? 0 : 8)

            ForEach(sessions) { session in
                NavigationLink(value: SessionRoute(
                    sessionId: session.id,
                    fallbackTitle: session.title,
                    fallbackSubtitle: session.identitySubtitle
                )) {
                    TimelineSessionCardRow(
                        session: session,
                        role: role,
                        connectivityBanner: connectivityBanner
                    )
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("timeline-session-row")
            }
        }
    }
}

/// What the timeline shows while the user is filtering.
///
/// The resident rows are the filter's corpus and stay on screen; the server
/// lane is a separate section, never a replacement for them. This carries the
/// counts alongside the remote state because the footer has to distinguish
/// "nothing here matches" from "nothing has been searched yet".
struct TimelineSearchPresentation {
    let query: String
    let visibleCount: Int
    let residentCount: Int
    let remote: TimelineSearchState
    /// Which lane produced ``remote``'s results, if any. Determines whether the
    /// meaning-search row is offered.
    let remoteLane: TimelineSearchLane?
    let onSearchAll: () -> Void
    let onSearchByMeaning: () -> Void
    let onRetry: () -> Void
}

private struct SessionRoute: Hashable {
    let sessionId: String
    let fallbackTitle: String
    let fallbackSubtitle: String?

    init(sessionId: String, fallbackTitle: String, fallbackSubtitle: String? = nil) {
        self.sessionId = sessionId
        self.fallbackTitle = fallbackTitle
        self.fallbackSubtitle = fallbackSubtitle
    }
}

struct TimelineSessionCardRow: View {
    let session: SessionSummary
    let role: TimelineRowRole
    var connectivityBanner: TimelineConnectivityBanner = .none

    var body: some View {
        let signal = TimelineSignal.resolve(for: session, suppressed: connectivityBanner != .none)
        let isNewResult = role == .newResult
        let dotColor = isNewResult ? newResultStatusColor(for: session) : signal.dotColor
        // Only the rows that want you carry an edge; everything else is a
        // quiet char card and lets the dot and status line speak. A new result
        // is marked by its bold title and outcome dot, never by gold, which
        // stays the brand and the primary action.
        let edgeColor: Color? = role == .needsYou ? signal.dotColor : nil
        let dotPulses = !isNewResult && signal.pulses
        let titleWeight: Font.Weight = isNewResult ? .bold : .semibold
        let titleLineLimit = role == .needsYou || isNewResult ? 2 : 1

        // Three-line row built for glanceability:
        //  - kicker: project · machine · branch ............... when
        //  - headline: ● <frozen server-resolved title>
        //  - status: demoted runtime state, colored by signal
        // The frozen `title` (server timeline_title) is the muscle-memory anchor;
        // the leading dot + status carry "is it active / waiting on me / done".
        // No Managed badge, no turns/tools — that was the dead right half.
        HStack(alignment: .top, spacing: 11) {
            ProviderGlyph(provider: session.provider, size: 30)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    if let project = session.projectLabel {
                        Text(project)
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(Ember.textSecondary)
                            .lineLimit(1)
                    }
                    if let machine = session.timelineMachineLabel {
                        Text("· \(machine)")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(Ember.textMuted)
                            .lineLimit(1)
                            .layoutPriority(-1)
                    }
                    if let branch = session.timelineBranchBadgeLabel {
                        Text("· \(branch)")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(Ember.textMuted)
                            .lineLimit(1)
                            .layoutPriority(-1)
                    }
                    Spacer(minLength: 6)
                    if !isNewResult, let duration = stateDurationLabel(for: session) {
                        Text(duration)
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(Ember.textMuted)
                            .monospacedDigit()
                    }
                }

                HStack(alignment: .firstTextBaseline, spacing: 7) {
                    LivenessDot(color: dotColor, pulsing: dotPulses)
                        .alignmentGuide(.firstTextBaseline) { d in d[VerticalAlignment.center] + 4 }
                    Text(session.title)
                        .font(.subheadline.weight(titleWeight))
                        .foregroundStyle(Ember.text)
                        .lineLimit(titleLineLimit)
                }
                // The dot is color-only; fold its meaning into the headline so
                // VoiceOver announces "Waiting on you" / "Working" rather than
                // leaving amber as the sole, invisible-to-VoiceOver code.
                .accessibilityElement(children: .combine)
                .accessibilityLabel(rowAccessibilityLabel(session: session, role: role, signal: signal))

                if isNewResult {
                    NewResultLine(session: session)
                } else {
                    CompactRuntimeLine(session: session, signal: signal)
                }

                // B-lite drift line: the live, drifting summary title parked on a
                // demoted, low-contrast line where movement is legitimate. The
                // frozen headline above stays put (muscle memory); this is the
                // "what is it doing now" channel, shown only while actively
                // working so it never churns under a resting row.
                if role == .open, signal == .working, let drift = session.driftTitle {
                    Text("now: \(drift)")
                        .font(.caption2)
                        .italic()
                        .foregroundStyle(Ember.textMuted)
                        .lineLimit(1)
                }
            }
        }
        .padding(.vertical, 11)
        .padding(.horizontal, 12)
        .background(Ember.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(alignment: .leading) {
            if let edgeColor {
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(edgeColor.opacity(0.85))
                    .frame(width: 3)
                    .padding(.vertical, 12)
            }
        }
        .overlay {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(edgeColor?.opacity(0.28) ?? Ember.hairline, lineWidth: 0.8)
        }
    }
}

private struct NewResultLine: View {
    let session: SessionSummary

    var body: some View {
        Text(newResultStatusText(for: session))
            .font(.caption.weight(.semibold))
            .foregroundStyle(newResultStatusColor(for: session))
            .lineLimit(1)
            .accessibilityLabel(newResultStatusText(for: session))
    }
}

private struct TimelineSearchResultRow: View {
    let session: SessionSummary
    let query: String

    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            ProviderGlyph(provider: session.provider, size: 30)

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    if let project = session.projectLabel {
                        Text(project)
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    if let machine = session.timelineMachineLabel {
                        Text("· \(machine)")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                    }
                    Spacer(minLength: 6)
                    Text(relativeTime(session.timelineAnchor))
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(.tertiary)
                }

                Text(session.title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(2)

                if let snippet = nonEmpty(session.matchSnippet) {
                    Text(highlightedSnippet(snippet, query: query))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }

                Text("\(session.providerLabel) · \(session.turnCount) turns")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 11)
        .padding(.horizontal, 12)
        .background(Ember.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(Ember.hairline, lineWidth: 0.8)
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
                    .foregroundStyle(Ember.flame)
                    .lineLimit(1)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(runtimeBadgeAccessibilityLabel(for: session, stale: sessionStale))
    }
}

/// Slim fault strip. Healthy = invisible, and "healthy" includes every
/// normal moment of a live stream: there is no "updating" state, because
/// the timeline is always updating. The strip only ever names a fault
/// (stale + failing, offline, signed out). Pull to refresh is the retry
/// path; this view is purely informational.
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
        case .degraded:
            return Style(label: "Connection degraded", symbol: "exclamationmark.triangle",
                         foreground: Ember.flame,
                         background: Ember.flame.opacity(0.12))
        case .offline:
            return Style(label: "Offline", symbol: "exclamationmark.triangle.fill",
                         foreground: Ember.ember,
                         background: Ember.ember.opacity(0.12))
        case .authRequired:
            return Style(label: "Sign in required", symbol: "person.crop.circle.badge.exclamationmark",
                         foreground: Ember.ember,
                         background: Ember.ember.opacity(0.12))
        }
    }
}


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
    func searchSessions(
        query: String,
        lane: TimelineSearchLane,
        daysBack: Int,
        limit: Int
    ) async throws -> [SessionSummary]
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

enum TimelineSearchState: Equatable {
    case idle
    case loading
    case empty
    case error(String)
    case loaded([SessionSummary])
}

@MainActor
final class TimelineViewModel: ObservableObject {
    @Published private(set) var state: TimelineLoadState = .initial
    @Published private(set) var searchState: TimelineSearchState = .idle
    /// Which lane produced ``searchState``'s results. Nil before the server has
    /// been asked, and cleared whenever the query changes.
    @Published private(set) var searchLane: TimelineSearchLane?
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
    private var searchGeneration: UInt64 = 0
    private var searchTask: Task<Void, Never>?
    private var lastSearchQuery: String?
    private var lastSearchLane: TimelineSearchLane?
    private let apiFactory: (String) -> TimelineSessionsClient?
    private let streamFactory: (URL, Int) -> TimelineSessionsStreamSource
    private let enableRealtime: Bool
    private let enableConnectivityClock: Bool
    private let limit = 40
    // Search reaches the corpus the list does not hold. Bounded to the same
    // window the timeline route allows, so the scope never silently widens past
    // what the screen is showing.
    private let searchDaysBack = timelineSearchScopeDays
    private let searchLimit = 30
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
        guard isInitial else {
            // A pushed detail stops this screen's stream. Refresh on return so
            // a read acknowledgement emitted while the detail was visible is
            // reflected immediately instead of waiting for the safety poll.
            await refresh(using: appState, reloadWidget: true)
            return
        }
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

    /// Back to "the server lane has not been asked". Called on every keystroke:
    /// the answer on screen belongs to the previous query, so it is dropped
    /// rather than left standing under a new one.
    func clearSearch() {
        cancelRemoteSearch()
    }

    func cancelRemoteSearch() {
        searchGeneration &+= 1
        searchTask?.cancel()
        searchTask = nil
        searchLane = nil
        searchState = .idle
    }

    /// Ask the server lane for the query currently on screen.
    ///
    /// Returns immediately; the work runs in ``searchTask`` so a keystroke can
    /// cancel it, and so the caller's button press never blocks the UI. The
    /// timeline keeps its own rows for the whole duration of this call.
    func searchRemote(query: String, lane: TimelineSearchLane, using appState: AppState) {
        let normalized = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalized.isEmpty else {
            cancelRemoteSearch()
            return
        }
        searchTask?.cancel()
        searchGeneration &+= 1
        let generation = searchGeneration
        lastSearchQuery = normalized
        lastSearchLane = lane
        searchLane = lane
        searchState = .loading

        searchTask = Task { [weak self] in
            guard let self else { return }
            guard let api = self.apiFactory(appState.serverURL) else {
                self.searchState = .error("Invalid server URL")
                return
            }
            do {
                let sessions = try await api.searchSessions(
                    query: normalized,
                    lane: lane,
                    daysBack: self.searchDaysBack,
                    limit: self.searchLimit
                )
                guard !Task.isCancelled, generation == self.searchGeneration else { return }
                self.searchState = sessions.isEmpty ? .empty : .loaded(sessions)
            } catch LonghouseAPIError.notAuthenticated {
                guard !Task.isCancelled, generation == self.searchGeneration else { return }
                self.applyConnectivity(.authFailed)
                appState.handleExpiredSession()
                self.searchState = .error("Sign in to search your sessions.")
            } catch {
                guard !Task.isCancelled, generation == self.searchGeneration else { return }
                let message = self.connectionBanner == .offline
                    ? "Search needs a connection."
                    : "Search failed: \(error.localizedDescription)"
                self.searchState = .error(message)
            }
        }
    }

    /// Await the in-flight server lane, for pull-to-refresh.
    func awaitRemoteSearch() async {
        await searchTask?.value
    }

    /// Repeat the last lane for the last query, so a retry after a failure or a
    /// pull-to-refresh does not silently change what is being asked.
    func retrySearch(using appState: AppState) {
        guard let query = lastSearchQuery, let lane = lastSearchLane else { return }
        searchRemote(query: query, lane: lane, using: appState)
    }

    var searchScopeDays: Int { searchDaysBack }

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
        // Keys first: parsing inside the comparator re-parsed each row's
        // anchor O(log n) times per upsert, on the main thread.
        current = current
            .enumerated()
            .map { (date: anchorDate(for: $0.element), index: $0.offset, session: $0.element) }
            .sorted { $0.date != $1.date ? $0.date > $1.date : $0.index < $1.index }
            .map(\.session)
        current = SessionSummary.residentCap(current, limit: limit)
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

private func newResultOutcomeLabel(for session: SessionSummary) -> String {
    switch session.stateFacts.lastResultOutcome?.lowercased() {
    case "failed": return "Failed"
    case "cancelled": return "Cancelled"
    default: return "Finished"
    }
}

private func newResultStatusText(for session: SessionSummary) -> String {
    let outcome = newResultOutcomeLabel(for: session)
    guard let date = parseLonghouseDate(session.stateFacts.lastResultAt) else { return outcome }
    return "\(outcome) · \(compactDuration(since: date)) ago"
}

private func newResultStatusColor(for session: SessionSummary) -> Color {
    switch session.stateFacts.lastResultOutcome?.lowercased() {
    case "failed": return Ember.ember
    case "cancelled": return Ember.textSecondary
    default: return Ember.sage
    }
}

private func rowAccessibilityLabel(
    session: SessionSummary,
    role: TimelineRowRole,
    signal: TimelineSignal
) -> String {
    if role == .newResult {
        return "\(session.title), new result, \(newResultStatusText(for: session))"
    }
    if role == .needsYou {
        return "\(session.title), needs you, \(signal.accessibilityState)"
    }
    return "\(session.title), \(signal.accessibilityState)"
}

private func highlightedSnippet(_ value: String, query: String) -> AttributedString {
    var attributed = AttributedString(value)
    let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !needle.isEmpty else { return attributed }

    var searchStart = value.startIndex
    while searchStart < value.endIndex,
          let match = value.range(
              of: needle,
              options: [.caseInsensitive, .diacriticInsensitive],
              range: searchStart..<value.endIndex
          ) {
        if let lower = AttributedString.Index(match.lowerBound, within: attributed),
           let upper = AttributedString.Index(match.upperBound, within: attributed) {
            attributed[lower..<upper].foregroundColor = .primary
            attributed[lower..<upper].backgroundColor = Color.accentColor.opacity(0.28)
        }
        searchStart = match.upperBound
    }
    return attributed
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
