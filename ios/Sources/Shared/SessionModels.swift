import Foundation

struct SessionSummary: Identifiable, Hashable, Codable, Sendable {
    let id: String
    let title: String
    let presenceState: String
    let provider: String?
    let project: String?
    let lastActivityAt: String?
    let summary: String?
    let userState: String?
    let status: String?
    let displayPhase: String?
    let presenceTool: String?
    let activeTool: String?
    let gitBranch: String?
    let homeLabel: String?
    let headOriginLabel: String?
    let timelineAnchorAt: String?
    let userMessages: Int?
    let toolCalls: Int?
    let liveControlAvailable: Bool?
    let hostReattachAvailable: Bool?
    let replyToLiveSessionAvailable: Bool?

    init(
        id: String,
        title: String,
        presenceState: String,
        provider: String?,
        project: String?,
        lastActivityAt: String?,
        summary: String? = nil,
        userState: String? = nil,
        status: String? = nil,
        displayPhase: String? = nil,
        presenceTool: String? = nil,
        activeTool: String? = nil,
        gitBranch: String? = nil,
        homeLabel: String? = nil,
        headOriginLabel: String? = nil,
        timelineAnchorAt: String? = nil,
        userMessages: Int? = nil,
        toolCalls: Int? = nil,
        liveControlAvailable: Bool? = nil,
        hostReattachAvailable: Bool? = nil,
        replyToLiveSessionAvailable: Bool? = nil
    ) {
        self.id = id
        self.title = title
        self.presenceState = presenceState
        self.provider = provider
        self.project = project
        self.lastActivityAt = lastActivityAt
        self.summary = summary
        self.userState = userState
        self.status = status
        self.displayPhase = displayPhase
        self.presenceTool = presenceTool
        self.activeTool = activeTool
        self.gitBranch = gitBranch
        self.homeLabel = homeLabel
        self.headOriginLabel = headOriginLabel
        self.timelineAnchorAt = timelineAnchorAt
        self.userMessages = userMessages
        self.toolCalls = toolCalls
        self.liveControlAvailable = liveControlAvailable
        self.hostReattachAvailable = hostReattachAvailable
        self.replyToLiveSessionAvailable = replyToLiveSessionAvailable
    }

    var isBlocked: Bool { presenceState == "blocked" }
    var isNeedsUser: Bool { presenceState == "needs_user" }
    var isUserActive: Bool { userState == nil || userState == "active" }
    var needsAttention: Bool { (isBlocked || isNeedsUser) && isUserActive }
    var isExecuting: Bool {
        presenceState == "thinking" || presenceState == "running" || status == "working" || status == "active"
    }
    var isIdle: Bool { presenceState == "idle" || status == "idle" }
    var attentionLabel: String { isBlocked ? "Needs permission" : "Waiting on you" }
    var timelineAnchor: String? { timelineAnchorAt ?? lastActivityAt }
    var turnCount: Int { userMessages ?? 0 }
    var toolCount: Int { toolCalls ?? 0 }

    var providerLabel: String {
        guard let provider, !provider.isEmpty else { return "Session" }
        return provider.prefix(1).uppercased() + provider.dropFirst()
    }

    var projectLabel: String {
        guard let project, !project.isEmpty else { return "Unknown project" }
        return project
    }

    var managementLabel: String {
        if liveControlAvailable == true || replyToLiveSessionAvailable == true {
            return "Live control"
        }
        if hostReattachAvailable == true {
            return "Reattach"
        }
        return "Unmanaged"
    }

    var displayPhaseLabel: String {
        if let displayPhase = displayPhase?.trimmingCharacters(in: .whitespacesAndNewlines), !displayPhase.isEmpty {
            return displayPhase
        }
        let tool = activeTool ?? presenceTool
        switch presenceState {
        case "running":
            return tool.map { "Running \($0)" } ?? "Running"
        case "thinking":
            return "Thinking"
        case "needs_user":
            return "Needs you"
        case "blocked":
            return tool.map { "Blocked on \($0)" } ?? "Needs permission"
        case "idle":
            return "Idle"
        default:
            if status == "completed" { return "Completed" }
            if status == "working" || status == "active" { return "Recent progress" }
            return "Recent"
        }
    }

    var summaryPreview: String? {
        guard let summary = summary?.trimmingCharacters(in: .whitespacesAndNewlines), !summary.isEmpty else {
            return nil
        }
        return summary
    }

    static func attentionWidgetOrder(_ sessions: [SessionSummary], limit: Int) -> [SessionSummary] {
        let active = sessions.filter(\.isUserActive)
        let attention = active.filter(\.needsAttention)
        let recent = active.filter { !$0.needsAttention }
        return Array((attention + recent).prefix(limit))
    }
}

struct SessionsResponse: Codable, Sendable {
    let sessions: [TimelineCard]
}

struct TimelineCard: Codable, Sendable {
    let head: TimelineSession
    let headOriginLabel: String?
}

struct TimelineSession: Codable, Sendable {
    let id: String
    let summaryTitle: String?
    let summary: String?
    let status: String?
    let presenceState: String?
    let presenceTool: String?
    let activeTool: String?
    let displayPhase: String?
    let userState: String?
    let provider: String?
    let project: String?
    let gitBranch: String?
    let homeLabel: String?
    let timelineAnchorAt: String?
    let lastActivityAt: String?
    let userMessages: Int?
    let toolCalls: Int?
    let capabilities: SessionCapabilities?
    let loopMode: SessionLoopMode?
}

struct SessionCapabilities: Codable, Sendable {
    let liveControlAvailable: Bool
    let hostReattachAvailable: Bool
    let replyToLiveSessionAvailable: Bool
}

enum SessionLoopMode: String, Codable, Sendable, CaseIterable, Hashable {
    case manual
    case assist
    case autopilot

    var label: String {
        switch self {
        case .manual: return "Manual"
        case .assist: return "Assist"
        case .autopilot: return "Autopilot"
        }
    }
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
    let displayPhase: String?
    let activeTool: String?
    let homeLabel: String?
    let originLabel: String?
    let capabilities: SessionCapabilities
    let loopMode: SessionLoopMode?

    var displayTitle: String {
        summaryTitle ?? summary ?? provider
    }

    var effectiveLoopMode: SessionLoopMode {
        loopMode ?? .manual
    }

    var canSendLive: Bool {
        capabilities.liveControlAvailable || capabilities.replyToLiveSessionAvailable
    }

    var isControlOffline: Bool {
        !canSendLive && capabilities.hostReattachAvailable
    }

    var isReadOnly: Bool {
        !canSendLive && !capabilities.hostReattachAvailable
    }

    var cockpitPhaseState: String {
        presenceState ?? status ?? "idle"
    }

    var cockpitPhaseLabel: String {
        if let displayPhase = displayPhase?.trimmingCharacters(in: .whitespacesAndNewlines), !displayPhase.isEmpty {
            return displayPhase
        }
        let tool = activeTool ?? presenceTool
        switch cockpitPhaseState {
        case "running":
            return tool.map { "Running \($0)" } ?? "Running"
        case "thinking":
            return "Thinking"
        case "needs_user":
            return "Needs you"
        case "blocked":
            return tool.map { "Blocked on \($0)" } ?? "Needs permission"
        case "working", "active":
            return "Working"
        case "completed":
            return "Completed"
        case "idle":
            return "Idle"
        default:
            return cockpitPhaseState.capitalized
        }
    }

    var controlHealthMessage: String? {
        if isControlOffline {
            return "Control is offline until the host reconnects."
        }
        if isReadOnly {
            return "Read-only imported session."
        }
        return nil
    }
}

struct SessionEvent: Codable, Identifiable, Sendable {
    let id: Int
    let role: String
    let contentText: String?
    let toolName: String?
    let toolInputJSON: [String: JSONValue]?
    let toolOutputText: String?
    let toolCallId: String?
    let timestamp: String
    let inActiveContext: Bool
    let isHeadBranch: Bool

    private enum CodingKeys: String, CodingKey {
        case id
        case role
        case contentText
        case toolName
        case toolInputJSON = "toolInputJson"
        case toolOutputText
        case toolCallId
        case timestamp
        case inActiveContext
        case isHeadBranch
    }

    /// Lookup a top-level key from the tool input JSON as a string.
    func toolInputString(_ key: String) -> String? {
        switch toolInputJSON?[key] {
        case .string(let s): return s
        case .int(let n): return String(n)
        case .double(let n): return String(n)
        case .bool(let b): return String(b)
        case .array, .object, .null, .none: return nil
        }
    }
}

/// Minimal JSON value type for decoding tool_input_json without losing shape.
enum JSONValue: Codable, Sendable, Hashable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case array([JSONValue])
    case object([String: JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null; return }
        if let v = try? c.decode(Bool.self) { self = .bool(v); return }
        if let v = try? c.decode(Int.self) { self = .int(v); return }
        if let v = try? c.decode(Double.self) { self = .double(v); return }
        if let v = try? c.decode(String.self) { self = .string(v); return }
        if let v = try? c.decode([JSONValue].self) { self = .array(v); return }
        if let v = try? c.decode([String: JSONValue].self) { self = .object(v); return }
        throw DecodingError.dataCorruptedError(in: c, debugDescription: "Unsupported JSON value")
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let v): try c.encode(v)
        case .int(let v): try c.encode(v)
        case .double(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        }
    }
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

struct DraftReplyResponse: Codable, Sendable {
    let draftText: String
    let model: String
    let generatedAt: String
    let basedOnEventIds: [Int]
}

struct LoopModeResponse: Codable, Sendable {
    let sessionId: String
    let loopMode: SessionLoopMode
}
