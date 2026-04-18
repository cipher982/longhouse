import Foundation

struct SessionSummary: Codable, Identifiable, Hashable, Sendable {
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

struct SessionCapabilities: Codable, Sendable {
    let liveControlAvailable: Bool
    let hostReattachAvailable: Bool
    let replyToLiveSessionAvailable: Bool
}

struct SessionDetail: Codable, Identifiable, Sendable {
    let id: String
    let provider: String
    let project: String?
    let cwd: String?
    let gitBranch: String?
    let summary: String?
    let summaryTitle: String?
    let presenceState: String?
    let presenceTool: String?
    let userState: String
    let status: String?
    let lastActivityAt: String?
    let homeLabel: String?
    let originLabel: String?
    let capabilities: SessionCapabilities

    var displayTitle: String {
        summaryTitle ?? summary ?? provider
    }
}

struct SessionEvent: Codable, Identifiable, Sendable {
    let id: Int
    let role: String
    let contentText: String?
    let toolName: String?
    let toolOutputText: String?
    let timestamp: String
    let inActiveContext: Bool
    let isHeadBranch: Bool
}

struct SessionEventsResponse: Codable, Sendable {
    let events: [SessionEvent]
    let total: Int
    let branchMode: String
}

struct SessionTurn: Codable, Identifiable, Sendable {
    let id: Int
    let sessionId: String
    let state: String
    let terminalPhase: String?
    let errorCode: String?
    let userSubmittedAt: String
    let terminalAt: String?
}

struct SessionTurnsResponse: Codable, Sendable {
    let turns: [SessionTurn]
    let total: Int
}
