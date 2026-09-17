import Foundation

/// Which retrieval lane a session search asked for.
///
/// The phone names exactly one lane per request and never substitutes one for
/// the other, so a result set always means what its label said. `lexical` is
/// the default: it is the cheapest lane that can reach a session the phone has
/// not loaded, and it carries the matched snippet the result row renders.
/// `semantic` is the dense paraphrase lane — the only one that finds a session
/// whose words the user did not type, and the only one worth waiting for.
enum TimelineSearchLane: String, Sendable, Equatable {
    case lexical
    case semantic
}

/// How far back the server lane looks. Matches the timeline route's own bound
/// (`days_back` is capped at 90 there), so the search cannot silently widen to
/// a window the list is not showing.
let timelineSearchScopeDays = 90

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
