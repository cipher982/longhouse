import Foundation

enum RuntimeDisplayText {
    private static let shellAliases: Set<String> = ["bash", "shell", "terminal"]

    static func canonicalToolLabel(_ value: String?) -> String? {
        guard let value = value?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else {
            return nil
        }
        if shellAliases.contains(value.lowercased()) {
            return "Shell"
        }
        return value
    }

    static func canonicalDisplayText(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if let running = canonicalPrefixedTool(in: trimmed, prefix: "Running ") {
            return running
        }
        if let blocked = canonicalPrefixedTool(in: trimmed, prefix: "Blocked on ") {
            return blocked
        }
        if let approval = canonicalPrefixedTool(in: trimmed, prefix: "Approval needed \u{2022} ") {
            return approval
        }
        return trimmed
    }

    static func canonicalDisplayText(_ value: String?) -> String? {
        guard let value else { return nil }
        let normalized = canonicalDisplayText(value)
        return normalized.isEmpty ? nil : normalized
    }

    private static func canonicalPrefixedTool(in value: String, prefix: String) -> String? {
        guard value.lowercased().hasPrefix(prefix.lowercased()) else {
            return nil
        }
        let tail = String(value.dropFirst(prefix.count))
        guard let canonicalTail = canonicalToolPhrase(tail) else {
            return value
        }
        return "\(prefix)\(canonicalTail)"
    }

    private static func canonicalToolPhrase(_ value: String) -> String? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if shellAliases.contains(trimmed.lowercased()) {
            return "Shell"
        }
        return nil
    }
}

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
    let runtimeDisplay: SessionRuntimeDisplay?

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
        replyToLiveSessionAvailable: Bool? = nil,
        runtimeDisplay: SessionRuntimeDisplay? = nil
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
        self.runtimeDisplay = runtimeDisplay
    }

    var isClosed: Bool {
        if runtimeDisplay?.lifecycle == "closed" { return true }
        if runtimeDisplay?.lifecycle == nil && status == "completed" { return true }
        return false
    }
    private var effectiveRuntimeState: String? {
        if let runtimeDisplay { return runtimeDisplay.state }
        return presenceState
    }
    var isBlocked: Bool { !isClosed && effectiveRuntimeState == "blocked" }
    var isNeedsUser: Bool { !isClosed && effectiveRuntimeState == "needs_user" }
    var isUserActive: Bool { userState == nil || userState == "active" }
    var needsAttention: Bool {
        if isClosed || !isUserActive { return false }
        if let runtimeDisplay { return runtimeDisplay.needsAttention }
        return isBlocked || isNeedsUser
    }
    var isExecuting: Bool {
        if isClosed { return false }
        if let runtimeDisplay { return runtimeDisplay.isExecuting }
        return presenceState == "thinking" || presenceState == "running" || status == "working" || status == "active"
    }
    var isIdle: Bool {
        if isClosed { return true }
        if let runtimeDisplay { return runtimeDisplay.isIdle }
        return presenceState == "idle" || status == "idle"
    }
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
        isManaged ? "Managed" : "Unmanaged"
    }

    var managementTone: String {
        return "neutral"
    }

    private var isManaged: Bool {
        if runtimeDisplay?.controlPath == "managed" { return true }
        if runtimeDisplay?.controlPath == "unmanaged" { return false }
        return liveControlAvailable == true || hostReattachAvailable == true || replyToLiveSessionAvailable == true
    }

    var displayPhaseLabel: String {
        if isClosed {
            return "Completed"
        }
        if let phaseLabel = runtimeDisplay?.phaseLabel.trimmingCharacters(in: .whitespacesAndNewlines), !phaseLabel.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(phaseLabel)
        }
        if let displayPhase = displayPhase?.trimmingCharacters(in: .whitespacesAndNewlines), !displayPhase.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(displayPhase)
        }
        let tool = RuntimeDisplayText.canonicalToolLabel(activeTool ?? presenceTool)
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
            // Phase 3 of session-liveness-honesty: prefer backend lifecycle
            // when available; fall back to status for older payloads only.
            let lifecycle = runtimeDisplay?.lifecycle
            if lifecycle == "closed" { return "Completed" }
            if lifecycle == nil && status == "completed" { return "Completed" }
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
    let runtimeDisplay: SessionRuntimeDisplay?
}

struct SessionCapabilities: Codable, Sendable {
    let liveControlAvailable: Bool
    let hostReattachAvailable: Bool
    let replyToLiveSessionAvailable: Bool
    let canQueueNextInput: Bool?
    let canSteerActiveTurn: Bool?
    let displayLabel: String?
    let displayDetail: String?
    let displayTone: String?
}

/// Outcome returned from POST /api/sessions/{id}/input.
///
/// - `sent`: Longhouse dispatched the message to the live session immediately.
/// - `queued`: The session was working; the message is durably queued and
///   will auto-send at the next safe turn boundary.
enum SessionInputOutcome: String, Codable, Sendable {
    case sent
    case queued
}

struct QueuedInputSummary: Codable, Sendable, Identifiable {
    let id: Int
    let text: String
    let intent: String
    let status: String
    let lastError: String?
    let createdAt: String?
}

struct SessionInputResponse: Codable, Sendable {
    let outcome: SessionInputOutcome
    let inputId: Int
    let intent: String
    let queued: [QueuedInputSummary]

    var pendingInputCount: Int {
        queued.filter { $0.status == "queued" || $0.status == "delivering" }.count
    }

    var visibleFailedInputCount: Int {
        queued.filter { row in
            row.status == "failed" && !(row.intent == "steer" && row.lastError == "turn_ended")
        }.count
    }
}

struct SessionRuntimeDisplay: Codable, Hashable, Sendable {
    let truthTier: String
    let state: String?
    let tone: String
    let headline: String
    let detail: String?
    let phaseLabel: String
    let compactToolLabel: String?
    let isLive: Bool
    let isExecuting: Bool
    let needsAttention: Bool
    let isIdle: Bool
    let heuristicActive: Bool
    let isManagedLocalTruth: Bool
    let hasSignal: Bool
    // Phase 2/3 of session-liveness-honesty: three orthogonal axes.
    // Optional so older backend payloads still decode cleanly.
    let controlPath: String?
    let activityRecency: String?
    let lifecycle: String?
    let hostState: String?
    let terminalReason: String?
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
    let runtimeDisplay: SessionRuntimeDisplay?
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

    var canQueueNextInput: Bool {
        capabilities.canQueueNextInput ?? false
    }

    var canSteerActiveTurn: Bool {
        capabilities.canSteerActiveTurn ?? false
    }

    var isControlOffline: Bool {
        !canSendLive && capabilities.hostReattachAvailable
    }

    var isReadOnly: Bool {
        !canSendLive && !capabilities.hostReattachAvailable
    }

    var runtimePhaseState: String {
        if let runtimeDisplay { return runtimeDisplay.state ?? "idle" }
        return presenceState ?? status ?? "idle"
    }

    var runtimePhaseLabel: String {
        if let phaseLabel = runtimeDisplay?.phaseLabel.trimmingCharacters(in: .whitespacesAndNewlines), !phaseLabel.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(phaseLabel)
        }
        if let displayPhase = displayPhase?.trimmingCharacters(in: .whitespacesAndNewlines), !displayPhase.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(displayPhase)
        }
        let tool = RuntimeDisplayText.canonicalToolLabel(activeTool ?? presenceTool)
        switch runtimePhaseState {
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
            return runtimePhaseState.capitalized
        }
    }

    var controlHealthMessage: String? {
        if isControlOffline {
            return capabilities.displayDetail ?? "Control is offline until the host reconnects."
        }
        if isReadOnly {
            return capabilities.displayDetail ?? "Search-only imported session."
        }
        return nil
    }

    var runtimeCapabilityLabel: String {
        if let label = capabilities.displayLabel?.trimmingCharacters(in: .whitespacesAndNewlines), !label.isEmpty {
            return label
        }
        if canSendLive { return "Live control" }
        if capabilities.hostReattachAvailable { return "Reattach" }
        return "Search only"
    }

    var runtimeCapabilityTone: String {
        if let tone = capabilities.displayTone?.trimmingCharacters(in: .whitespacesAndNewlines), !tone.isEmpty {
            return tone
        }
        if canSendLive { return "success" }
        if capabilities.hostReattachAvailable { return "warning" }
        return "neutral"
    }

    var runtimeHeadline: String {
        if let headline = runtimeDisplay?.headline.trimmingCharacters(in: .whitespacesAndNewlines), !headline.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(headline)
        }
        if isControlOffline || isReadOnly { return runtimeCapabilityLabel }
        if isSessionExecuting { return "Working" }
        if runtimePhaseState == "idle" { return "Ready" }
        return runtimePhaseLabel
    }

    var runtimeDetail: String? {
        if let detail = runtimeDisplay?.detail?.trimmingCharacters(in: .whitespacesAndNewlines), !detail.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(detail)
        }
        if isControlOffline || isReadOnly {
            return controlHealthMessage
        }
        if runtimePhaseState == "idle" {
            return controlHealthMessage
        }
        if runtimeHeadline != runtimePhaseLabel {
            return runtimePhaseLabel
        }
        return controlHealthMessage
    }

    var runtimeTone: String {
        if let tone = runtimeDisplay?.tone { return tone }
        switch runtimePhaseState {
        case "running": return "running"
        case "thinking": return "thinking"
        case "needs_user": return "needs-user"
        case "blocked": return "blocked"
        case "idle", "completed": return "idle"
        case "working", "active": return "inferred"
        default: return "inactive"
        }
    }

    var isSessionExecuting: Bool {
        runtimeDisplay?.isExecuting == true || runtimePhaseState == "running" || runtimePhaseState == "thinking"
    }
}

struct SessionThreadResponse: Codable, Sendable {
    let rootSessionId: String
    let headSessionId: String
    let sessions: [SessionDetail]
}

struct SessionProjectionItem: Codable, Identifiable, Sendable {
    let kind: String
    let sessionId: String
    let timestamp: String
    let event: SessionEvent?
    let continuedFromSessionId: String?
    let continuationKind: String?
    let originLabel: String?
    let parentOriginLabel: String?
    let parentContinuationKind: String?
    let branchedFromEventId: Int?

    var id: String {
        if kind == "event", let event {
            return "event:\(event.id)"
        }
        return "seam:\(sessionId):\(timestamp)"
    }
}

struct SessionProjectionResponse: Codable, Sendable {
    let rootSessionId: String
    let focusSessionId: String
    let headSessionId: String
    let pathSessionIds: [String]
    let items: [SessionProjectionItem]
    let total: Int
    let pageOffset: Int
    let branchMode: String
    let abandonedEvents: Int
}

struct SessionWorkspaceResponse: Codable, Sendable {
    let session: SessionDetail
    let thread: SessionThreadResponse
    let projection: SessionProjectionResponse

    var events: [SessionEvent] {
        projection.items.compactMap(\.event)
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
