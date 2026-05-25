import Foundation

let transcriptSyncState = "syncing_transcript"

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
        if let running = canonicalPrefixedTool(in: trimmed, prefix: "Running ", outputPrefix: "Using ") {
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

    private static func canonicalPrefixedTool(in value: String, prefix: String, outputPrefix: String? = nil) -> String? {
        guard value.lowercased().hasPrefix(prefix.lowercased()) else {
            return nil
        }
        let tail = String(value.dropFirst(prefix.count))
        guard let canonicalTail = canonicalToolPhrase(tail) else {
            return value
        }
        return "\(outputPrefix ?? prefix)\(canonicalTail)"
    }

    private static func canonicalToolPhrase(_ value: String) -> String? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if shellAliases.contains(trimmed.lowercased()) {
            return "Shell"
        }
        return nil
    }

}

struct HostObservation: Codable, Hashable, Sendable {
    let state: String
    let lastSeenAt: String?
    let source: String?
}

struct ProcessObservation: Codable, Hashable, Sendable {
    let status: String
    let pid: Int?
    let processStartTime: String?
    let observedAt: String?
    let lastSeenAt: String?
    let sourceMtime: String?
    let sourcePath: String?
    let reason: String?
    let source: String?
}

struct PhaseObservation: Codable, Hashable, Sendable {
    let kind: String?
    let tool: String?
    let source: String?
    let observedAt: String?
    let expiresAt: String?
}

struct ActivityObservation: Codable, Hashable, Sendable {
    let lastTranscriptAt: String?
    let lastRuntimeSignalAt: String?
    let lastProgressAt: String?
}

struct LifecycleFact: Codable, Hashable, Sendable {
    let state: String
    let reason: String?
    let observedAt: String?
}

struct ControlObservation: Codable, Hashable, Sendable {
    let state: String?
    let reason: String?
    let source: String?
    let lastSeenAt: String?
    let expiresAt: String?
    let transport: String?
}

struct SessionLivenessFacts: Codable, Hashable, Sendable {
    let controlPath: String
    let control: ControlObservation?
    let processState: String?
    let host: HostObservation
    let process: ProcessObservation
    let phase: PhaseObservation
    let activity: ActivityObservation
    let lifecycle: LifecycleFact

    init(
        controlPath: String,
        control: ControlObservation? = nil,
        processState: String?,
        host: HostObservation,
        process: ProcessObservation,
        phase: PhaseObservation,
        activity: ActivityObservation,
        lifecycle: LifecycleFact
    ) {
        self.controlPath = controlPath
        self.control = control
        self.processState = processState
        self.host = host
        self.process = process
        self.phase = phase
        self.activity = activity
        self.lifecycle = lifecycle
    }
}

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
    let runtimeDisplay: SessionRuntimeDisplay?
    let runtimeFacts: SessionLivenessFacts?
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
        runtimeDisplay: SessionRuntimeDisplay? = nil,
        runtimeFacts: SessionLivenessFacts? = nil,
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
        self.runtimeFacts = runtimeFacts
        self.timelineCard = timelineCard
    }

    var isClosed: Bool {
        if runtimeDisplay?.lifecycle == "closed" { return true }
        if runtimeDisplay?.lifecycle != nil { return false }
        if runtimeDisplay?.lifecycle == nil && status == "completed" { return true }
        return false
    }

    private var effectiveRuntimeState: String? {
        if let runtimeDisplay { return runtimeDisplay.state }
        return presenceState
    }
    var isBlocked: Bool { !isClosed && effectiveRuntimeState == "blocked" }
    var isUserActive: Bool { userState == nil || userState == "active" }
    var needsAttention: Bool {
        if isClosed || !isUserActive { return false }
        if let runtimeDisplay { return runtimeDisplay.needsAttention }
        return isBlocked
    }
    var isExecuting: Bool {
        if isClosed { return false }
        if let runtimeDisplay { return runtimeDisplay.isExecuting }
        return false
    }
    var isIdle: Bool {
        if isClosed { return true }
        if let runtimeDisplay { return runtimeDisplay.isIdle }
        return false
    }
    var runtimeTone: String {
        if let tone = runtimeDisplay?.tone { return tone }
        return isClosed ? "closed" : "inactive"
    }
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

    private var isManaged: Bool {
        if runtimeDisplay?.controlPath == "managed" { return true }
        if runtimeDisplay?.controlPath == "unmanaged" { return false }
        return liveControlAvailable == true || hostReattachAvailable == true || replyToLiveSessionAvailable == true
    }

    var displayPhaseLabel: String {
        if isClosed {
            return "Closed"
        }
        if let phaseLabel = runtimeDisplay?.phaseLabel.trimmingCharacters(in: .whitespacesAndNewlines), !phaseLabel.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(phaseLabel)
        }
        return "Inactive"
    }

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

struct SessionRuntimeDisplay: Codable, Hashable, Sendable {
    let truthTier: String
    var signalTier: String? = nil
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
    let isManagedLocalTruth: Bool
    let hasSignal: Bool
    let controlPath: String?
    let activityRecency: String?
    let lifecycle: String?
    let hostState: String?
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
    let runtimeDisplay: SessionRuntimeDisplay?
    let runtimeFacts: SessionLivenessFacts?
    let loopMode: SessionLoopMode?
    var transcriptPreview: SessionTranscriptPreview? = nil

    var displayTitle: String {
        summaryTitle ?? summary ?? provider
    }

    var effectiveLoopMode: SessionLoopMode {
        loopMode ?? .manual
    }

    var isClosed: Bool {
        if runtimeDisplay?.lifecycle == "closed" { return true }
        if runtimeDisplay?.lifecycle != nil { return false }
        if runtimeDisplay?.lifecycle == nil && status == "completed" { return true }
        return false
    }

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

    var runtimePhaseState: String {
        if let runtimeDisplay { return runtimeDisplay.state ?? "idle" }
        return "idle"
    }

    var runtimePhaseLabel: String {
        if let phaseLabel = runtimeDisplay?.phaseLabel.trimmingCharacters(in: .whitespacesAndNewlines), !phaseLabel.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(phaseLabel)
        }
        return "Inactive"
    }

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

    var runtimeHeadline: String {
        if let headline = runtimeDisplay?.headline.trimmingCharacters(in: .whitespacesAndNewlines), !headline.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(headline)
        }
        if isControlOffline || isReadOnly { return runtimeCapabilityLabel }
        return "Inactive"
    }

    var runtimeDetail: String? {
        if let runtimeDisplay {
            guard let detail = runtimeDisplay.detail?.trimmingCharacters(in: .whitespacesAndNewlines), !detail.isEmpty else {
                return nil
            }
            return RuntimeDisplayText.canonicalDisplayText(detail)
        }
        if isControlOffline || isReadOnly {
            return controlHealthMessage
        }
        return controlHealthMessage
    }

    var runtimeTone: String {
        if let tone = runtimeDisplay?.tone { return tone }
        return isClosed ? "closed" : "inactive"
    }

    var isSessionExecuting: Bool {
        if let runtimeDisplay { return runtimeDisplay.isExecuting }
        return false
    }

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
            runtimeFacts: runtimeFacts,
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
    let inputOrigin: SessionInputOrigin?

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
