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

    static func compactFactToolLabel(_ value: String?) -> String? {
        guard let value = value?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else {
            return nil
        }
        let canonical = value.split(separator: "__").last.map(String.init) ?? value
        let withoutPrefixes = canonical
            .replacingOccurrences(of: #"^(hatch_|tool_|mcp_)"#, with: "", options: .regularExpression)
        let normalized = withoutPrefixes
            .replacingOccurrences(of: #"[-_.]+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalized.isEmpty else { return nil }
        switch normalized.lowercased() {
        case "codex": return "Codex"
        case "claude": return "Claude"
        case "antigravity": return "Antigravity"
        case "gemini": return "Gemini"
        case "default": return "Z.ai"
        case "shell", "bash", "terminal": return "Shell"
        case "edit", "write", "patch", "apply patch", "file change", "filechange": return "Edit"
        default: return normalized.capitalized
        }
    }

    static func phaseStatusLabel(kind: String, tool: String?) -> String {
        let phase = kind == "needs_user" ? "idle" : kind.replacingOccurrences(of: #"[-_]+"#, with: " ", options: .regularExpression)
        if let compactTool = compactFactToolLabel(tool), kind == "running" {
            return "Using \(compactTool)"
        }
        if let compactTool = compactFactToolLabel(tool), kind == "blocked" {
            return "\(phase.capitalized) \(compactTool)"
        }
        return phase.capitalized
    }

    static func phaseTone(_ kind: String) -> String {
        switch kind {
        case "thinking", "running", "blocked", "stalled":
            return kind
        case "idle", "needs_user":
            return "idle"
        default:
            return "inactive"
        }
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

struct SessionFactStatus: Hashable, Sendable {
    let label: String
    let tone: String
    let seenAt: String?
    let seenAtPrefix: String
}

func sessionFactStatus(_ facts: SessionLivenessFacts?) -> SessionFactStatus? {
    guard let facts else { return nil }
    let processState = facts.processState ?? "unknown"
    if facts.lifecycle.state == "closed" || processState == "closed" {
        return SessionFactStatus(
            label: "Closed",
            tone: "closed",
            seenAt: facts.lifecycle.observedAt ?? facts.phase.observedAt ?? facts.activity.lastTranscriptAt,
            seenAtPrefix: "Closed"
        )
    }
    if let kind = facts.phase.kind?.trimmingCharacters(in: .whitespacesAndNewlines), !kind.isEmpty {
        return SessionFactStatus(
            label: RuntimeDisplayText.phaseStatusLabel(kind: kind, tool: facts.phase.tool),
            tone: RuntimeDisplayText.phaseTone(kind),
            seenAt: facts.phase.observedAt,
            seenAtPrefix: "Updated"
        )
    }
    if processState == "running" || facts.process.status == "observed" {
        return SessionFactStatus(
            label: "Running",
            tone: "inactive",
            seenAt: facts.process.observedAt ?? facts.process.lastSeenAt,
            seenAtPrefix: "Verified"
        )
    }
    return SessionFactStatus(
        label: "No live signal",
        tone: "inactive",
        seenAt: facts.activity.lastRuntimeSignalAt,
        seenAtPrefix: facts.activity.lastRuntimeSignalAt == nil ? "Checked" : "Last signal"
    )
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
        if let runtimeFacts { return runtimeFacts.lifecycle.state == "closed" }
        if runtimeDisplay?.lifecycle == "closed" { return true }
        if runtimeDisplay?.lifecycle == nil && status == "completed" { return true }
        return false
    }

    private var effectiveRuntimeState: String? {
        if runtimeDisplay?.state == transcriptSyncState { return transcriptSyncState }
        if runtimeFacts != nil { return nil }
        if let runtimeDisplay { return runtimeDisplay.state }
        return presenceState
    }
    var isBlocked: Bool { !isClosed && effectiveRuntimeState == "blocked" }
    var isUserActive: Bool { userState == nil || userState == "active" }
    var needsAttention: Bool {
        if runtimeFacts != nil { return false }
        if isClosed || !isUserActive { return false }
        if let runtimeDisplay { return runtimeDisplay.needsAttention }
        return isBlocked
    }
    var isExecuting: Bool {
        if runtimeFacts != nil { return false }
        if isClosed { return false }
        if let runtimeDisplay { return runtimeDisplay.isExecuting }
        return presenceState == "thinking" || presenceState == "running"
    }
    var isIdle: Bool {
        if runtimeFacts != nil { return false }
        if isClosed { return true }
        if let runtimeDisplay { return runtimeDisplay.isIdle }
        return presenceState == "idle" || status == "idle"
    }
    var runtimeTone: String {
        if runtimeDisplay?.state == transcriptSyncState { return runtimeDisplay?.tone ?? "active" }
        if let factStatus = sessionFactStatus(runtimeFacts) { return factStatus.tone }
        if isClosed { return "idle" }
        if let tone = runtimeDisplay?.tone { return tone }
        switch presenceState {
        case "running": return "running"
        case "thinking": return "thinking"
        case "needs_user", "idle": return "idle"
        case "blocked": return "blocked"
        default: return "inactive"
        }
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
        if runtimeFacts?.controlPath == "managed" { return "Managed" }
        if runtimeFacts?.controlPath == "unmanaged" { return "Unmanaged" }
        if let label = timelineCard?.ownership.label.trimmingCharacters(in: .whitespacesAndNewlines), !label.isEmpty {
            return label
        }
        return isManaged ? "Managed" : "Unmanaged"
    }

    var managementTone: String {
        return timelineCard?.ownership.tone ?? "neutral"
    }

    private var isManaged: Bool {
        if runtimeFacts?.controlPath == "managed" { return true }
        if runtimeFacts?.controlPath == "unmanaged" { return false }
        if runtimeDisplay?.controlPath == "managed" { return true }
        if runtimeDisplay?.controlPath == "unmanaged" { return false }
        return liveControlAvailable == true || hostReattachAvailable == true || replyToLiveSessionAvailable == true
    }

    var displayPhaseLabel: String {
        if runtimeDisplay?.state == transcriptSyncState,
           let phaseLabel = runtimeDisplay?.phaseLabel.trimmingCharacters(in: .whitespacesAndNewlines),
           !phaseLabel.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(phaseLabel)
        }
        if let factStatus = sessionFactStatus(runtimeFacts) {
            return factStatus.label
        }
        if isClosed {
            return "Closed"
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
            return tool.map { "Using \($0)" } ?? "Running"
        case "thinking":
            return "Thinking"
        case "needs_user":
            return "Idle"
        case "blocked":
            return tool.map { "Blocked on \($0)" } ?? "Needs permission"
        case "idle":
            return "Idle"
        default:
            let lifecycle = runtimeDisplay?.lifecycle
            if lifecycle == "closed" { return "Closed" }
            if lifecycle == nil && status == "completed" { return "Closed" }
            return "Inactive"
        }
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
    let timestamp: String
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
            timestamp: timestamp,
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil
        )
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
        if let runtimeFacts { return runtimeFacts.lifecycle.state == "closed" }
        if runtimeDisplay?.lifecycle == "closed" { return true }
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
        if runtimeDisplay?.state == transcriptSyncState { return transcriptSyncState }
        if let runtimeFacts {
            let kind = runtimeFacts.phase.kind?.trimmingCharacters(in: .whitespacesAndNewlines)
            return kind?.isEmpty == false ? kind! : "unknown"
        }
        if let runtimeDisplay { return runtimeDisplay.state ?? "idle" }
        return presenceState ?? status ?? "idle"
    }

    var runtimePhaseLabel: String {
        if runtimeDisplay?.state == transcriptSyncState,
           let phaseLabel = runtimeDisplay?.phaseLabel.trimmingCharacters(in: .whitespacesAndNewlines),
           !phaseLabel.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(phaseLabel)
        }
        if let factStatus = sessionFactStatus(runtimeFacts) {
            return factStatus.label
        }
        if let phaseLabel = runtimeDisplay?.phaseLabel.trimmingCharacters(in: .whitespacesAndNewlines), !phaseLabel.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(phaseLabel)
        }
        if let displayPhase = displayPhase?.trimmingCharacters(in: .whitespacesAndNewlines), !displayPhase.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(displayPhase)
        }
        let tool = RuntimeDisplayText.canonicalToolLabel(activeTool ?? presenceTool)
        switch runtimePhaseState {
        case "running":
            return tool.map { "Using \($0)" } ?? "Running"
        case "thinking":
            return "Thinking"
        case "needs_user":
            return "Idle"
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
        if runtimeDisplay?.state == transcriptSyncState,
           let headline = runtimeDisplay?.headline.trimmingCharacters(in: .whitespacesAndNewlines),
           !headline.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(headline)
        }
        if let factStatus = sessionFactStatus(runtimeFacts) {
            return factStatus.label
        }
        if let headline = runtimeDisplay?.headline.trimmingCharacters(in: .whitespacesAndNewlines), !headline.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(headline)
        }
        if isControlOffline || isReadOnly { return runtimeCapabilityLabel }
        if isSessionExecuting { return "Working" }
        if runtimePhaseState == "idle" { return "Idle" }
        return runtimePhaseLabel
    }

    var runtimeDetail: String? {
        if runtimeDisplay?.state == transcriptSyncState,
           let detail = runtimeDisplay?.detail?.trimmingCharacters(in: .whitespacesAndNewlines),
           !detail.isEmpty {
            return RuntimeDisplayText.canonicalDisplayText(detail)
        }
        if runtimeFacts != nil {
            return nil
        }
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
        if runtimeDisplay?.state == transcriptSyncState { return runtimeDisplay?.tone ?? "active" }
        if let factStatus = sessionFactStatus(runtimeFacts) { return factStatus.tone }
        if let tone = runtimeDisplay?.tone { return tone }
        switch runtimePhaseState {
        case "running": return "running"
        case "thinking": return "thinking"
        case "needs_user": return "idle"
        case "blocked": return "blocked"
        case "idle", "completed": return "idle"
        default: return "inactive"
        }
    }

    var isSessionExecuting: Bool {
        if runtimeFacts != nil {
            return runtimePhaseState == "running" || runtimePhaseState == "thinking"
        }
        return runtimeDisplay?.isExecuting == true || runtimePhaseState == "running" || runtimePhaseState == "thinking"
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
