import Foundation

struct SessionSummary: Codable, Identifiable, Sendable {
    let id: String
    let title: String
    let presenceState: String
    let provider: String?
    let project: String?
    let lastActivityAt: String?

    var isBlocked: Bool { presenceState == "blocked" }
    var isNeedsUser: Bool { presenceState == "needs_user" }
    var attentionLabel: String { isBlocked ? "Needs permission" : "Waiting on you" }
}

struct SessionsResponse: Codable, Sendable {
    let sessions: [TimelineCard]
}

struct TimelineCard: Codable, Sendable {
    let head: TimelineSession
}

struct TimelineSession: Codable, Sendable {
    let id: String
    let summaryTitle: String?
    let summary: String?
    let presenceState: String?
    let userState: String?
    let provider: String?
    let project: String?
    let lastActivityAt: String?
}
