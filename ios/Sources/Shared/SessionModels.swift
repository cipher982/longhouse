import Foundation

let transcriptSyncState = "syncing_transcript"

/// Honest summarization state — mirrors backend `summary_status` field.
/// Tiebreaker: ready > pending > failed > unavailable.
enum SummaryStatus: String, Codable, Sendable, Hashable {
    case ready
    case pending
    case failed
    case unavailable
}

struct SessionSummary: Identifiable, Hashable, Codable, Sendable {
    let id: String
    // Thread identity used by the timeline stream for upsert/remove dedup.
    // Optional so cached payloads from before this field existed still decode.
    let threadId: String?
    let title: String
    let presenceState: String
    let provider: String?
    let project: String?
    let lastActivityAt: String?
    let summary: String?
    let summaryStatus: String?
    let firstUserMessage: String?
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
    let runtimeDisplay: SessionRuntimeDisplay
    let timelineCard: TimelineCardPresentation?

    init(
        id: String,
        threadId: String? = nil,
        title: String,
        presenceState: String,
        provider: String?,
        project: String?,
        lastActivityAt: String?,
        summary: String? = nil,
        summaryStatus: String? = nil,
        firstUserMessage: String? = nil,
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
        runtimeDisplay: SessionRuntimeDisplay,
        timelineCard: TimelineCardPresentation? = nil
    ) {
        self.id = id
        self.threadId = threadId
        self.title = title
        self.presenceState = presenceState
        self.provider = provider
        self.project = project
        self.lastActivityAt = lastActivityAt
        self.summary = summary
        self.summaryStatus = summaryStatus
        self.firstUserMessage = firstUserMessage
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
        self.timelineCard = timelineCard
    }

    var isClosed: Bool { runtimeDisplay.lifecycle == "closed" }

    var isBlocked: Bool { !isClosed && runtimeDisplay.state == "blocked" }
    var isUserActive: Bool { userState == nil || userState == "active" }
    var needsAttention: Bool {
        if isClosed || !isUserActive { return false }
        return runtimeDisplay.needsAttention
    }
    var isExecuting: Bool { !isClosed && runtimeDisplay.isExecuting }
    var isIdle: Bool { isClosed || runtimeDisplay.isIdle }
    var runtimeTone: String { runtimeDisplay.tone }
    var timelineAnchor: String? { timelineAnchorAt ?? lastActivityAt }
    var timelineBranchBadgeLabel: String? {
        guard let branch = gitBranch?.trimmingCharacters(in: .whitespacesAndNewlines), !branch.isEmpty else {
            return nil
        }
        if branch.caseInsensitiveCompare("HEAD") == .orderedSame {
            return nil
        }
        return branch
    }
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
        if let label = timelineCard?.ownership.label.trimmingCharacters(in: .whitespacesAndNewlines), !label.isEmpty {
            return label
        }
        return isManaged ? "Managed" : "Unmanaged"
    }

    var managementTone: String {
        return timelineCard?.ownership.tone ?? "neutral"
    }

    private var isManaged: Bool { runtimeDisplay.controlPath == "managed" }

    var displayPhaseLabel: String { runtimeDisplay.phaseLabel }

    var timelineStatusLabel: String {
        if let label = timelineCard?.status.label.trimmingCharacters(in: .whitespacesAndNewlines), !label.isEmpty {
            return label
        }
        return "No live signal"
    }

    var timelineStatusSeenAt: String? {
        if let seenAt = timelineCard?.status.seenAt?.trimmingCharacters(in: .whitespacesAndNewlines), !seenAt.isEmpty {
            return seenAt
        }
        return nil
    }

    var timelineStatusSeenAtPrefix: String {
        if let prefix = timelineCard?.status.seenAtPrefix.trimmingCharacters(in: .whitespacesAndNewlines), !prefix.isEmpty {
            return prefix
        }
        return "Checked"
    }

    var timelineStatusTone: String {
        if let tone = timelineCard?.status.tone.trimmingCharacters(in: .whitespacesAndNewlines), !tone.isEmpty {
            return tone
        }
        return "inactive"
    }

    var timelineBorderTone: String {
        if let tone = timelineCard?.borderTone.trimmingCharacters(in: .whitespacesAndNewlines), !tone.isEmpty {
            return tone
        }
        return timelineStatusTone
    }

    var summaryPreview: String? {
        guard let summary = summary?.trimmingCharacters(in: .whitespacesAndNewlines), !summary.isEmpty else {
            return nil
        }
        return summary
    }

    var firstUserPreview: String? {
        guard let firstUserMessage = firstUserMessage?.trimmingCharacters(in: .whitespacesAndNewlines), !firstUserMessage.isEmpty else {
            return nil
        }
        return firstUserMessage
    }

    var timelineSummaryPreview: String? {
        if let summaryPreview {
            return summaryPreview
        }
        guard let firstUserPreview else { return nil }
        return firstUserPreview == title.trimmingCharacters(in: .whitespacesAndNewlines) ? nil : firstUserPreview
    }

    /// Decoded summary lifecycle. Falls back to inferring from `summary` when
    /// the backend hasn't supplied an explicit status (older payloads).
    var summaryStatusValue: SummaryStatus {
        if let raw = summaryStatus, let value = SummaryStatus(rawValue: raw) {
            return value
        }
        return summaryPreview != nil ? .ready : .unavailable
    }

    static func attentionWidgetOrder(_ sessions: [SessionSummary], limit: Int) -> [SessionSummary] {
        let active = sessions.filter(\.isUserActive)
        let attention = active.filter(\.needsAttention)
        let recent = active.filter { !$0.needsAttention }
        return Array((attention + recent).prefix(limit))
    }
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
    let inputMode: String?
    let defaultInputIntent: String?
    let composerEnabled: Bool?
    let composerPlaceholder: String?
    let composerDisabledReason: String?
    let sendDisabledReason: String?
    let attachImages: Bool?
}

struct TimelineBadgePresentation: Codable, Hashable, Sendable {
    let label: String
    let tone: String
}

struct TimelineStatusPresentation: Codable, Hashable, Sendable {
    let label: String
    let tone: String
    let seenAt: String?
    let seenAtPrefix: String
}

struct TimelineCardPresentation: Codable, Hashable, Sendable {
    let ownership: TimelineBadgePresentation
    let status: TimelineStatusPresentation
    let borderTone: String
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

enum SessionInputIntent: String, Codable, Sendable {
    case auto
    case queue
    case steer
}

enum SessionInputStatus: String, Codable, Sendable {
    case queued
    case delivering
    case delivered
    case cancelled
    case failed
}

struct QueuedInputSummary: Codable, Sendable, Identifiable {
    let id: Int
    let text: String
    let intent: SessionInputIntent
    let status: SessionInputStatus
    let lastError: String?
    let createdAt: String?
}

struct SessionInputResponse: Codable, Sendable {
    let outcome: SessionInputOutcome
    let inputId: Int
    let clientRequestId: String?
    let intent: SessionInputIntent
    let queued: [QueuedInputSummary]

    var pendingInputCount: Int {
        queued.filter { $0.status == .queued || $0.status == .delivering }.count
    }

    var visibleFailedInputCount: Int {
        queued.filter { row in
            row.status == .failed && !(row.intent == .steer && row.lastError == "turn_ended")
        }.count
    }
}

extension SessionRuntimeDisplay {
    /// Synthetic placeholder for SwiftUI previews and widget snapshot fixtures.
    /// Production runtimeDisplay always comes from the server projection.
    static func widgetPlaceholder(
        state: String?,
        phase: String,
        tone: String,
        lifecycle: String = "open"
    ) -> SessionRuntimeDisplay {
        SessionRuntimeDisplay(
            truthTier: "none",
            signalTier: "none",
            state: state,
            tone: tone,
            headline: phase,
            detail: nil,
            phaseLabel: phase,
            compactToolLabel: nil,
            isLive: false,
            isExecuting: false,
            needsAttention: false,
            isIdle: lifecycle == "closed",
            isStalled: false,
            isManagedLocalTruth: false,
            hasSignal: false,
            controlPath: "unmanaged",
            activityRecency: "none",
            lifecycle: lifecycle,
            hostState: "unknown",
            terminalReason: nil
        )
    }
}

struct SessionRuntimeDisplay: Codable, Hashable, Sendable {
    let truthTier: String
    let signalTier: String
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
    let isStalled: Bool
    let isManagedLocalTruth: Bool
    let hasSignal: Bool
    let controlPath: String
    let activityRecency: String
    let lifecycle: String
    let hostState: String
    let terminalReason: String?
}

struct SessionTranscriptPreview: Codable, Hashable, Sendable {
    let eventId: Int
    let text: String
    let eventOrigin: String
    let timestamp: String?
    let isProvisional: Bool
    let isComplete: Bool?
    let contentCursor: String?
    let isStale: Bool?
    let staleReason: String?

    var shouldRender: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && isStale != true
    }

    var syntheticEvent: SessionEvent {
        SessionEvent(
            id: -abs(eventId),
            role: "assistant",
            contentText: text,
            toolName: nil,
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: nil,
            toolCallState: nil,
            timestamp: timestamp ?? "",
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil
        )
    }
}

enum TranscriptPreviewProjection {
    static func visibleEvents(
        durableEvents: [SessionEvent],
        preview: SessionTranscriptPreview?
    ) -> [SessionEvent] {
        guard let preview, preview.shouldRender else { return durableEvents }
        let previewText = preview.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !previewText.isEmpty else { return durableEvents }
        guard let previewTimestamp = preview.timestamp else { return durableEvents }

        if let lastDurableAssistant = durableEvents.reversed().first(where: {
            $0.role == "assistant" && ($0.contentText ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false
        }) {
            let lastText = (lastDurableAssistant.contentText ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if lastText == previewText { return durableEvents }
        }

        if let previewAt = LonghouseDateParser.parse(previewTimestamp),
           let latestEvent = durableEvents.last,
           let latestDurableAt = LonghouseDateParser.parse(latestEvent.timestamp),
           latestDurableAt >= previewAt {
            return durableEvents
        }

        return durableEvents + [preview.syntheticEvent]
    }
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
    let runtimeDisplay: SessionRuntimeDisplay
    let loopMode: SessionLoopMode?
    var transcriptPreview: SessionTranscriptPreview? = nil

    var displayTitle: String {
        summaryTitle ?? summary ?? provider
    }

    var effectiveLoopMode: SessionLoopMode {
        loopMode ?? .manual
    }

    var isClosed: Bool { runtimeDisplay.lifecycle == "closed" }

    var canSendLive: Bool {
        if isClosed { return false }
        return capabilities.composerEnabled ?? (capabilities.liveControlAvailable || capabilities.replyToLiveSessionAvailable)
    }

    /// Codex managed sessions advertise `attach_images=true` once both the
    /// backend and the engine on this device support the attach pipeline.
    var attachImagesEnabled: Bool {
        canSendLive && (capabilities.attachImages ?? false)
    }

    var inputModeValue: String? {
        guard let mode = capabilities.inputMode?.trimmingCharacters(in: .whitespacesAndNewlines),
              !mode.isEmpty else {
            return nil
        }
        return mode.lowercased()
    }

    var canQueueNextInput: Bool {
        capabilities.canQueueNextInput ?? false
    }

    var canSteerActiveTurn: Bool {
        capabilities.canSteerActiveTurn ?? false
    }

    var isControlOffline: Bool {
        if inputModeValue == "offline" { return true }
        if inputModeValue == "read_only" { return false }
        return !canSendLive && capabilities.hostReattachAvailable
    }

    var isReadOnly: Bool {
        if inputModeValue == "read_only" { return true }
        if inputModeValue == "offline" { return false }
        return !canSendLive && !capabilities.hostReattachAvailable
    }

    var runtimePhaseState: String { runtimeDisplay.state ?? "idle" }

    var runtimePhaseLabel: String { runtimeDisplay.phaseLabel }

    var controlHealthMessage: String? {
        if let disabledReason = capabilities.composerDisabledReason?.trimmingCharacters(in: .whitespacesAndNewlines),
           !disabledReason.isEmpty {
            return disabledReason
        }
        if isControlOffline {
            return capabilities.displayDetail ?? "Control is offline until the host reconnects."
        }
        if isReadOnly {
            return capabilities.displayDetail ?? "Read-only imported session."
        }
        return nil
    }

    var runtimeCapabilityLabel: String {
        if let label = capabilities.displayLabel?.trimmingCharacters(in: .whitespacesAndNewlines), !label.isEmpty {
            if label.caseInsensitiveCompare("Live control") == .orderedSame { return "Send" }
            if label.caseInsensitiveCompare("Search only") == .orderedSame { return "Read only" }
            return label
        }
        if canSendLive { return "Send" }
        if isControlOffline { return "Control offline" }
        return "Read only"
    }

    var runtimeCapabilityTone: String {
        if let tone = capabilities.displayTone?.trimmingCharacters(in: .whitespacesAndNewlines), !tone.isEmpty {
            return tone
        }
        if canSendLive { return "success" }
        if isControlOffline { return "warning" }
        return "neutral"
    }

    var defaultInputIntent: String {
        guard let intent = capabilities.defaultInputIntent?.trimmingCharacters(in: .whitespacesAndNewlines),
              ["auto", "steer", "queue"].contains(intent) else {
            return "auto"
        }
        return intent
    }

    var composerPlaceholder: String {
        guard let placeholder = capabilities.composerPlaceholder?.trimmingCharacters(in: .whitespacesAndNewlines),
              !placeholder.isEmpty else {
            return "Reply"
        }
        return placeholder
    }

    var runtimeHeadline: String { runtimeDisplay.headline }

    var runtimeDetail: String? {
        guard let detail = runtimeDisplay.detail?.trimmingCharacters(in: .whitespacesAndNewlines), !detail.isEmpty else {
            return nil
        }
        return detail
    }

    var runtimeTone: String { runtimeDisplay.tone }

    var isSessionExecuting: Bool { runtimeDisplay.isExecuting }

    func replacingTranscriptPreview(_ transcriptPreview: SessionTranscriptPreview?) -> SessionDetail {
        SessionDetail(
            id: id,
            provider: provider,
            project: project,
            cwd: cwd,
            gitBranch: gitBranch,
            summary: summary,
            summaryTitle: summaryTitle,
            presenceState: presenceState,
            presenceTool: presenceTool,
            userState: userState,
            status: status,
            lastActivityAt: lastActivityAt,
            displayPhase: displayPhase,
            activeTool: activeTool,
            homeLabel: homeLabel,
            originLabel: originLabel,
            capabilities: capabilities,
            runtimeDisplay: runtimeDisplay,
            loopMode: loopMode,
            transcriptPreview: transcriptPreview
        )
    }

    var withoutTranscriptPreview: SessionDetail {
        replacingTranscriptPreview(nil)
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

struct SessionMobileTailResponse: Codable, Sendable {
    let session: SessionDetail
    let projection: SessionProjectionResponse
    let snapshotEventId: Int?

    var events: [SessionEvent] {
        projection.items.compactMap(\.event)
    }
}

enum SessionInputAuthoredVia: Codable, Hashable, Sendable {
    case longhouse
    case terminal
    case unknown(String)

    init(serverValue: String) {
        switch serverValue {
        case "longhouse":
            self = .longhouse
        case "terminal":
            self = .terminal
        default:
            self = .unknown(serverValue)
        }
    }

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        self.init(serverValue: value)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .longhouse:
            try container.encode("longhouse")
        case .terminal:
            try container.encode("terminal")
        case .unknown(let value):
            try container.encode(value)
        }
    }
}

struct SessionInputOrigin: Codable, Hashable, Sendable {
    let authoredVia: SessionInputAuthoredVia
    let sessionInputId: Int?
    let clientRequestId: String?
}

enum ToolCallState: String, Codable, Hashable, Sendable, CaseIterable {
    case running
    case completed
    case dropped
}

struct SessionEvent: Codable, Identifiable, Sendable {
    let id: Int
    let role: String
    let contentText: String?
    let toolName: String?
    let toolInputJSON: [String: JSONValue]?
    let toolOutputText: String?
    let toolCallId: String?
    let toolCallState: ToolCallState?
    let timestamp: String
    let inActiveContext: Bool
    let isHeadBranch: Bool
    let inputOrigin: SessionInputOrigin?

    private enum CodingKeys: String, CodingKey {
        case id
        case role
        case contentText
        case toolName
        case toolInputJSON = "toolInputJson"
        case toolOutputText
        case toolCallId
        case toolCallState
        case timestamp
        case inActiveContext
        case isHeadBranch
        case inputOrigin
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
    let sessionInputId: Int?
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
