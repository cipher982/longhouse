import Foundation
import SwiftUI

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
    // Live, drifting summary title — used for the subordinate "now:" drift line,
    // NOT the headline. The stable headline is `title` (server timeline_title).
    let summaryTitle: String?
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
        summaryTitle: String? = nil,
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
        self.summaryTitle = summaryTitle
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

    /// Live, drifting summary title for the subordinate "now:" drift line.
    /// Suppressed when it would just echo the frozen headline (`title`).
    var driftTitle: String? {
        guard let drift = summaryTitle?.trimmingCharacters(in: .whitespacesAndNewlines), !drift.isEmpty else {
            return nil
        }
        return drift == title.trimmingCharacters(in: .whitespacesAndNewlines) ? nil : drift
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

/// The single attention axis for a timeline row, shared by the app card and the
/// home-screen widget (and mirrored on web in lib/sessionRuntime.ts). Three
/// semantic stops the user can read pre-attentively, plus a closed/quiet rest:
///   - attention: the session is WAITING ON YOU - steady amber, never pulses.
///   - working:   the session is actively running - teal, breathing (live only).
///   - quiet:     idle/stale - grey, static.
///   - closed:    ended - dimmed grey, static.
/// Provider identity color stays on the glyph; it never bleeds into this axis.
enum TimelineSignal {
    case attention
    case working
    case quiet
    case closed

    /// Amber for "needs you". Separated from teal/grey on luminance + hue so it
    /// survives colorblindness; the status label text is the redundant code.
    static let amber = Color(red: 0.91, green: 0.64, blue: 0.24)
    static let teal = Color(red: 0.24, green: 0.71, blue: 0.78)

    /// The leading dot color - the loudest at-a-glance signal.
    var dotColor: Color {
        switch self {
        case .attention: return Self.amber
        case .working: return Self.teal
        case .quiet: return .secondary
        case .closed: return .secondary.opacity(0.6)
        }
    }

    /// Card edge/accent. Quiet by default ("dark cockpit"): only the row that
    /// wants you lights up, so it pops by contrast rather than a wall of color.
    var accentColor: Color {
        switch self {
        case .attention: return Self.amber
        case .working: return Self.teal.opacity(0.8)
        case .quiet: return .secondary.opacity(0.4)
        case .closed: return .secondary.opacity(0.3)
        }
    }

    /// Status-label text color, demoted relative to the dot.
    var statusColor: Color {
        switch self {
        case .attention: return Self.amber
        case .working: return Self.teal
        case .closed: return .secondary.opacity(0.7)
        case .quiet: return .secondary
        }
    }

    /// Motion is reserved for genuine live work. "Waiting on you" is a stable
    /// state, so attention is steady, not pulsing - avoids alarm fatigue.
    var pulses: Bool { self == .working }

    /// Spoken equivalent of the dot color, so the attention axis reaches
    /// VoiceOver instead of being color-only.
    var accessibilityState: String {
        switch self {
        case .attention: return "Waiting on you"
        case .working: return "Working"
        case .quiet: return "Idle"
        case .closed: return "Closed"
        }
    }

    /// Resolve the attention signal from a session's runtime facts. The optional
    /// `suppressed` flag lets a surface force `.quiet` (e.g. the app suppresses
    /// per-row attention while a global connectivity banner owns severity).
    /// `needs_attention` (curated) drives amber, not the raw needs_user state.
    static func resolve(for session: SessionSummary, suppressed: Bool = false) -> TimelineSignal {
        if session.isClosed { return .closed }
        if suppressed { return .quiet }
        if session.needsAttention { return .attention }

        let tone = session.timelineStatusTone.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let live = session.runtimeDisplay.activityRecency == "live"
        switch tone {
        case "thinking", "running":
            // Only animate genuinely live work; a stale "running" must not pulse.
            return live ? .working : .quiet
        case "blocked", "stalled":
            return .attention
        default:
            return .quiet
        }
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
    var id: String {
        liveInputId ?? archiveInputId.map(String.init) ?? text
    }

    let archiveInputId: Int?
    let liveInputId: String?
    let text: String
    let intent: SessionInputIntent
    let status: SessionInputStatus
    let lastError: String?
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case archiveInputId = "id"
        case liveInputId
        case text
        case intent
        case status
        case lastError
        case createdAt
    }

    init(
        id: Int?,
        liveInputId: String? = nil,
        text: String,
        intent: SessionInputIntent,
        status: SessionInputStatus,
        lastError: String?,
        createdAt: String?
    ) {
        self.archiveInputId = id
        self.liveInputId = liveInputId
        self.text = text
        self.intent = intent
        self.status = status
        self.lastError = lastError
        self.createdAt = createdAt
    }
}

struct SessionInputResponse: Codable, Sendable {
    let outcome: SessionInputOutcome
    let inputId: Int?
    let liveInputId: String?
    let clientRequestId: String?
    let intent: SessionInputIntent
    let queued: [QueuedInputSummary]

    init(
        outcome: SessionInputOutcome,
        inputId: Int?,
        liveInputId: String? = nil,
        clientRequestId: String?,
        intent: SessionInputIntent,
        queued: [QueuedInputSummary]
    ) {
        self.outcome = outcome
        self.inputId = inputId
        self.liveInputId = liveInputId
        self.clientRequestId = clientRequestId
        self.intent = intent
        self.queued = queued
    }

    var pendingInputCount: Int {
        queued.filter { $0.status == .queued || $0.status == .delivering }.count
    }

    var visibleFailedInputCount: Int {
        queued.filter { row in
            row.status == .failed && !(row.intent == .steer && row.lastError == "turn_ended")
        }.count
    }
}

struct SessionPauseQuestionOption: Codable, Hashable, Sendable {
    let label: String
    let description: String?
    let value: String?
}

struct SessionPauseQuestion: Codable, Hashable, Sendable {
    let id: String
    let header: String?
    let question: String
    let multiSelect: Bool
    let options: [SessionPauseQuestionOption]
}

struct SessionPauseRequest: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let sessionId: String
    let runtimeKey: String
    let kind: String
    let status: String
    let provider: String
    let canRespond: Bool
    let title: String?
    let summary: String?
    let toolName: String?
    let questions: [SessionPauseQuestion]
    let occurredAt: String?
    let lastSeenAt: String?
    let resolvedAt: String?
    let expiresAt: String?

    var isPending: Bool { status == "pending" }
}

struct PauseRequestResponse: Codable, Sendable {
    let status: String
    let pauseRequest: SessionPauseRequest
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
            terminalReason: nil,
            pauseRequest: nil
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
    let pauseRequest: SessionPauseRequest?

    init(
        truthTier: String,
        signalTier: String,
        state: String?,
        tone: String,
        headline: String,
        detail: String?,
        phaseLabel: String,
        compactToolLabel: String?,
        isLive: Bool,
        isExecuting: Bool,
        needsAttention: Bool,
        isIdle: Bool,
        isStalled: Bool,
        isManagedLocalTruth: Bool,
        hasSignal: Bool,
        controlPath: String,
        activityRecency: String,
        lifecycle: String,
        hostState: String,
        terminalReason: String?,
        pauseRequest: SessionPauseRequest? = nil
    ) {
        self.truthTier = truthTier
        self.signalTier = signalTier
        self.state = state
        self.tone = tone
        self.headline = headline
        self.detail = detail
        self.phaseLabel = phaseLabel
        self.compactToolLabel = compactToolLabel
        self.isLive = isLive
        self.isExecuting = isExecuting
        self.needsAttention = needsAttention
        self.isIdle = isIdle
        self.isStalled = isStalled
        self.isManagedLocalTruth = isManagedLocalTruth
        self.hasSignal = hasSignal
        self.controlPath = controlPath
        self.activityRecency = activityRecency
        self.lifecycle = lifecycle
        self.hostState = hostState
        self.terminalReason = terminalReason
        self.pauseRequest = pauseRequest
    }
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
            inputOrigin: nil,
            eventOrigin: eventOrigin
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
    var activePauseRequest: SessionPauseRequest? {
        guard !isClosed, let request = runtimeDisplay.pauseRequest, request.isPending else {
            return nil
        }
        return request
    }

    var shouldShowAttentionFallback: Bool {
        guard !isClosed, activePauseRequest == nil else { return false }
        return runtimeDisplay.needsAttention
            || runtimeDisplay.state == "blocked"
            || runtimeDisplay.tone == "blocked"
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

struct SessionWorkspaceRevision: Codable, Hashable, Sendable {
    let latestEventId: Int?
    let latestSessionUpdatedAt: String?
    let latestRuntimeSignalAt: String?
    let runtimeVersionSum: Int?
    let pauseRequestCount: Int?
    let pauseRequestFingerprint: String?
    let managedControlCount: Int?
    let managedControlFingerprint: String?
    let livePreviewUpdatedAt: String?
    let threadSessionCount: Int?
    let fingerprint: String
}

struct SessionWorkspaceResponse: Codable, Sendable {
    let session: SessionDetail
    let thread: SessionThreadResponse
    let projection: SessionProjectionResponse
    var workspaceRevision: SessionWorkspaceRevision? = nil

    var events: [SessionEvent] {
        projection.items.compactMap(\.event)
    }
}

struct SessionMobileTailResponse: Codable, Sendable {
    let session: SessionDetail
    let projection: SessionProjectionResponse
    let snapshotEventId: Int?
    var workspaceRevision: SessionWorkspaceRevision? = nil

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

struct SessionEventMediaRef: Codable, Hashable, Sendable {
    let sha256: String
    let mediaState: String
    let mimeType: String?
    let byteSize: Int?
    let blobUrl: String
    let thumbUrl: String?
    let sourcePath: String?
    let sourceOffset: Int?
    let jsonPointer: String?
    let originalKind: String
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
    let eventOrigin: String?
    let mediaRefs: [SessionEventMediaRef]

    init(
        id: Int,
        role: String,
        contentText: String?,
        toolName: String?,
        toolInputJSON: [String: JSONValue]?,
        toolOutputText: String?,
        toolCallId: String?,
        toolCallState: ToolCallState?,
        timestamp: String,
        inActiveContext: Bool,
        isHeadBranch: Bool,
        inputOrigin: SessionInputOrigin?,
        eventOrigin: String? = nil,
        mediaRefs: [SessionEventMediaRef] = []
    ) {
        self.id = id
        self.role = role
        self.contentText = contentText
        self.toolName = toolName
        self.toolInputJSON = toolInputJSON
        self.toolOutputText = toolOutputText
        self.toolCallId = toolCallId
        self.toolCallState = toolCallState
        self.timestamp = timestamp
        self.inActiveContext = inActiveContext
        self.isHeadBranch = isHeadBranch
        self.inputOrigin = inputOrigin
        self.eventOrigin = eventOrigin
        self.mediaRefs = mediaRefs
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(Int.self, forKey: .id)
        role = try container.decode(String.self, forKey: .role)
        contentText = try container.decodeIfPresent(String.self, forKey: .contentText)
        toolName = try container.decodeIfPresent(String.self, forKey: .toolName)
        toolInputJSON = try container.decodeIfPresent([String: JSONValue].self, forKey: .toolInputJSON)
        toolOutputText = try container.decodeIfPresent(String.self, forKey: .toolOutputText)
        toolCallId = try container.decodeIfPresent(String.self, forKey: .toolCallId)
        toolCallState = try container.decodeIfPresent(ToolCallState.self, forKey: .toolCallState)
        timestamp = try container.decode(String.self, forKey: .timestamp)
        inActiveContext = try container.decode(Bool.self, forKey: .inActiveContext)
        isHeadBranch = try container.decode(Bool.self, forKey: .isHeadBranch)
        inputOrigin = try container.decodeIfPresent(SessionInputOrigin.self, forKey: .inputOrigin)
        eventOrigin = try container.decodeIfPresent(String.self, forKey: .eventOrigin)
        mediaRefs = try container.decodeIfPresent([SessionEventMediaRef].self, forKey: .mediaRefs) ?? []
    }

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
        case eventOrigin
        case mediaRefs
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
