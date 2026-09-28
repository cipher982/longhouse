import OSLog
import SwiftUI
import WidgetKit

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
                    detail: "All indexed history, including sessions not loaded here",
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
                    Text("No session in your indexed history matches “\(search.query)”.")
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

struct SessionRoute: Hashable {
    let sessionId: String
    let fallbackTitle: String
    let fallbackSubtitle: String?

    init(sessionId: String, fallbackTitle: String, fallbackSubtitle: String? = nil) {
        self.sessionId = sessionId
        self.fallbackTitle = fallbackTitle
        self.fallbackSubtitle = fallbackSubtitle
    }
}
