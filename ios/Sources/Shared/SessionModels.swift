import Foundation
import SwiftUI

struct SessionStateLabel: Hashable, Codable, Sendable {
    let key: String
    let label: String
    let tone: String
    let observedAt: String?
}

struct SessionStateAction: Hashable, Codable, Sendable {
    let state: String
    let reason: String?

    var isAvailable: Bool { state == "available" }
}

struct SessionStateFacts: Hashable, Codable, Sendable {
    let contractVersion: Int
    let presentationPolicyVersion: Int
    let mode: String
    let dispositionState: String
    let launchState: String?
    let runLifecycle: String?
    let activityState: String
    let activityTool: String?
    let activityObservedAt: String?
    let activityValidUntil: String?
    let controlOwnership: String
    let controlConnection: String
    let sendInput: SessionStateAction
    let interrupt: SessionStateAction
    let terminate: SessionStateAction
    let reattach: SessionStateAction
    let resume: SessionStateAction
    let pendingInteractionKind: String?
    let transcriptConvergence: String
    let primary: SessionStateLabel?
    let access: SessionStateLabel?
    let transcript: SessionStateLabel?

    static let unknown = SessionStateFacts(
        contractVersion: 1,
        presentationPolicyVersion: 1,
        mode: "unknown",
        dispositionState: "unknown",
        launchState: nil,
        runLifecycle: nil,
        activityState: "unknown",
        activityTool: nil,
        activityObservedAt: nil,
        activityValidUntil: nil,
        controlOwnership: "unowned",
        controlConnection: "unknown",
        sendInput: SessionStateAction(state: "unknown", reason: "missing_state_facts"),
        interrupt: SessionStateAction(state: "unknown", reason: "missing_state_facts"),
        terminate: SessionStateAction(state: "unknown", reason: "missing_state_facts"),
        reattach: SessionStateAction(state: "unknown", reason: "missing_state_facts"),
        resume: SessionStateAction(state: "unknown", reason: "missing_state_facts"),
        pendingInteractionKind: nil,
        transcriptConvergence: "unknown",
        primary: nil,
        access: nil,
        transcript: nil
    )
}

/// Keeps pre-contract on-device caches readable while every in-memory model
/// still has a non-optional facts object. Network DTOs require `session_state`;
/// this compatibility seam is only for Codable domain snapshots and old test
/// payloads, and never reconstructs facts from legacy display aliases.
@propertyWrapper
struct DefaultUnknownSessionStateFacts: Hashable, Codable, Sendable {
    var wrappedValue: SessionStateFacts

    init(wrappedValue: SessionStateFacts = .unknown) {
        self.wrappedValue = wrappedValue
    }

    init(from decoder: Decoder) throws {
        wrappedValue = try SessionStateFacts(from: decoder)
    }

    func encode(to encoder: Encoder) throws {
        try wrappedValue.encode(to: encoder)
    }
}

extension KeyedDecodingContainer {
    func decode(
        _ type: DefaultUnknownSessionStateFacts.Type,
        forKey key: Key
    ) throws -> DefaultUnknownSessionStateFacts {
        try decodeIfPresent(type, forKey: key) ?? DefaultUnknownSessionStateFacts()
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
    @DefaultUnknownSessionStateFacts var stateFacts: SessionStateFacts

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
        timelineCard: TimelineCardPresentation? = nil,
        stateFacts: SessionStateFacts = .unknown
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
        self.stateFacts = stateFacts
    }

    var isClosed: Bool { stateFacts.dispositionState == "closed" }

    var isBlocked: Bool { !isClosed && stateFacts.activityState == "blocked" }
    var isUserActive: Bool { userState == nil || userState == "active" }
    var needsAttention: Bool {
        if isClosed || !isUserActive { return false }
        return stateFacts.pendingInteractionKind != nil
    }
    var isExecuting: Bool {
        !isClosed && ["thinking", "executing"].contains(stateFacts.activityState)
    }
    var isIdle: Bool { isClosed || stateFacts.activityState == "quiescent" }
    var runtimeTone: String { stateFacts.primary?.tone ?? "inactive" }
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
        return stateFacts.controlOwnership == "owned" ? "Managed" : "Unmanaged"
    }

    var managementTone: String {
        "neutral"
    }

    private var isManaged: Bool { stateFacts.controlOwnership == "owned" }

    var displayPhaseLabel: String { stateFacts.primary?.label ?? "" }

    var timelineStatusLabel: String {
        if let label = stateFacts.primary?.label.trimmingCharacters(in: .whitespacesAndNewlines), !label.isEmpty {
            return label
        }
        return ""
    }

    var timelineStatusSeenAt: String? {
        if let seenAt = stateFacts.primary?.observedAt?.trimmingCharacters(in: .whitespacesAndNewlines), !seenAt.isEmpty {
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
        if let tone = stateFacts.primary?.tone.trimmingCharacters(in: .whitespacesAndNewlines), !tone.isEmpty {
            return tone
        }
        return "inactive"
    }

    var shouldAnnotateTimelineStatusAsStale: Bool {
        !isClosed
            && timelineStatusTone.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == "inactive"
            && stateFacts.activityState == "unknown"
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
    /// Pending interaction and provider activity are independent facts.
    static func resolve(for session: SessionSummary, suppressed: Bool = false) -> TimelineSignal {
        if session.isClosed { return .closed }
        if suppressed { return .quiet }
        if session.needsAttention { return .attention }

        switch session.stateFacts.activityState {
        case "thinking", "executing":
            return .working
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
    let stalenessReason: String?
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
            id: "synthetic:preview:\(eventId)",
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
    let title: String?
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
    @DefaultUnknownSessionStateFacts var stateFacts: SessionStateFacts
    var transcriptPreview: SessionTranscriptPreview? = nil

    var displayTitle: String {
        if let title = title?.trimmingCharacters(in: .whitespacesAndNewlines), !title.isEmpty {
            return title
        }
        if let summaryTitle = summaryTitle?.trimmingCharacters(in: .whitespacesAndNewlines), !summaryTitle.isEmpty {
            return summaryTitle
        }
        if let summary = summary?.trimmingCharacters(in: .whitespacesAndNewlines), !summary.isEmpty {
            return summary
        }
        return provider
    }

    var effectiveLoopMode: SessionLoopMode {
        loopMode ?? .manual
    }

    var isClosed: Bool { stateFacts.dispositionState == "closed" }
    var activePauseRequest: SessionPauseRequest? {
        guard !isClosed,
              stateFacts.pendingInteractionKind != nil,
              let request = runtimeDisplay.pauseRequest,
              request.isPending else {
            return nil
        }
        return request
    }

    var shouldShowAttentionFallback: Bool {
        guard !isClosed, activePauseRequest == nil else { return false }
        return stateFacts.pendingInteractionKind != nil
            || stateFacts.activityState == "blocked"
    }

    var canSendLive: Bool {
        if isClosed { return false }
        return capabilities.composerEnabled == true || stateFacts.sendInput.isAvailable
    }

    /// The archive is still converging with a live/catalog session. This is a
    /// distinct transcript state: an empty projection here is not evidence that
    /// the session has never produced messages.
    var isTranscriptSyncing: Bool {
        stateFacts.transcriptConvergence == "lagging"
    }

    var canDraftBeforeSendReady: Bool {
        guard !isClosed, !canSendLive else { return false }
        guard stateFacts.controlOwnership == "owned" else { return false }
        return stateFacts.launchState == "pending" || stateFacts.launchState == "dispatched"
    }

    /// Codex managed sessions advertise `attach_images=true` once both the
    /// backend and the engine on this device support the attach pipeline.
    var attachImagesEnabled: Bool {
        canSendLive && (capabilities.attachImages ?? false)
    }

    var canQueueNextInput: Bool {
        capabilities.canQueueNextInput ?? false
    }

    var canSteerActiveTurn: Bool {
        capabilities.canSteerActiveTurn ?? false
    }

    var isControlOffline: Bool {
        stateFacts.controlOwnership == "owned"
            && !canSendLive
            && ["degraded", "disconnected", "unknown"].contains(stateFacts.controlConnection)
    }

    var isReadOnly: Bool {
        !canSendLive && !isControlOffline
    }

    var runtimePhaseState: String { stateFacts.activityState }

    var runtimePhaseLabel: String { stateFacts.primary?.label ?? "" }

    var controlHealthMessage: String? {
        if isClosed { return "This session is closed." }
        if stateFacts.launchState == "pending" || stateFacts.launchState == "dispatched" {
            return "Session is still starting."
        }
        if isControlOffline {
            return "Control is offline until the host reconnects."
        }
        if isReadOnly {
            return stateFacts.controlOwnership == "owned"
                ? "This managed session is read-only."
                : "Read-only imported session."
        }
        return nil
    }

    var runtimeCapabilityLabel: String {
        if stateFacts.launchState == "pending" || stateFacts.launchState == "dispatched" {
            return "Launching"
        }
        if canSendLive { return "Send" }
        if let label = stateFacts.access?.label.trimmingCharacters(in: .whitespacesAndNewlines), !label.isEmpty {
            if label.caseInsensitiveCompare("Live control") == .orderedSame { return "Send" }
            if label.caseInsensitiveCompare("Search only") == .orderedSame { return "Read only" }
            return label
        }
        if isControlOffline { return "Control offline" }
        return "Read only"
    }

    var runtimeCapabilityTone: String {
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

    var runtimeHeadline: String { stateFacts.primary?.label ?? "" }

    var runtimeDetail: String? {
        guard let detail = stateFacts.transcript?.label.trimmingCharacters(in: .whitespacesAndNewlines), !detail.isEmpty else {
            return nil
        }
        return detail
    }

    var launchSetupStatusLabel: String {
        let providerName = provider.trimmingCharacters(in: .whitespacesAndNewlines)
        let providerLabel = providerName.isEmpty ? "session" : providerName.prefix(1).uppercased() + providerName.dropFirst()
        let fallback = providerLabel == "session" ? "Setting up session" : "Setting up \(providerLabel)"
        guard canDraftBeforeSendReady else { return fallback }
        if stateFacts.launchState == "pending" || stateFacts.launchState == "dispatched" {
            return fallback
        }
        guard var message = controlHealthMessage?.trimmingCharacters(in: .whitespacesAndNewlines),
              !message.isEmpty else {
            return fallback
        }
        while message.last == "." {
            message.removeLast()
        }
        return message.isEmpty ? fallback : message
    }

    var runtimeTone: String { stateFacts.primary?.tone ?? "inactive" }

    var isSessionExecuting: Bool {
        ["thinking", "executing"].contains(stateFacts.activityState)
    }

    func replacingTranscriptPreview(_ transcriptPreview: SessionTranscriptPreview?) -> SessionDetail {
        SessionDetail(
            id: id,
            title: title,
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
            stateFacts: DefaultUnknownSessionStateFacts(wrappedValue: stateFacts),
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

struct SessionAction: Codable, Hashable, Sendable {
    let id: String
    let kind: String
    let provider: String?
    let source: String
    let providerReason: String?
    let eventId: Int?
}

struct SessionProjectionItem: Codable, Identifiable, Sendable {
    let kind: String
    let sessionId: String
    let timestamp: String
    let event: SessionEvent?
    var action: SessionAction? = nil
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
        if kind == "action", let action {
            return action.id
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
    var generationId: String? = nil
    var nextCursor: String? = nil
    var hasMore: Bool? = nil
}

@propertyWrapper
struct FlexibleStringID: Codable, Hashable, Sendable {
    var wrappedValue: String?

    init(wrappedValue: String?) {
        self.wrappedValue = wrappedValue
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            wrappedValue = nil
        } else if let value = try? container.decode(String.self) {
            wrappedValue = value
        } else if let value = try? container.decode(Int.self) {
            wrappedValue = String(value)
        } else {
            throw DecodingError.typeMismatch(
                String.self,
                .init(codingPath: decoder.codingPath, debugDescription: "Expected string or integer identity")
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(wrappedValue)
    }
}

struct SessionWorkspaceRevision: Codable, Hashable, Sendable {
    @FlexibleStringID var latestEventId: String?
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
    @FlexibleStringID var snapshotEventId: String?
    var workspaceRevision: SessionWorkspaceRevision? = nil

    var events: [SessionEvent] {
        projection.items.compactMap(\.event)
    }
}

/// One immutable-render transcript page. `nextCursor` is opaque and already
/// generation-qualified by the Runtime Host; clients must never parse or
/// compare it.
struct SessionEventsPage: Codable, Sendable {
    let v: Int
    let sessionId: String
    let generationId: String
    let events: [SessionEvent]
    let nextCursor: String?
    let hasMore: Bool
    let total: Int
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
    /// Durable identity, not chronology. Storage-v2 IDs are opaque strings;
    /// legacy integer IDs decode to their decimal representation.
    let id: String
    /// Opaque generation-qualified paging cursor supplied by storage-v2.
    let cursor: String?
    /// Explicit durable ordering value when the API supplies one.
    let orderTimeUs: Int64?
    let threadId: String?
    let branchKind: String?
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
        id: String,
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
        mediaRefs: [SessionEventMediaRef] = [],
        cursor: String? = nil,
        orderTimeUs: Int64? = nil,
        threadId: String? = nil,
        branchKind: String? = nil
    ) {
        self.id = id
        self.cursor = cursor
        self.orderTimeUs = orderTimeUs
        self.threadId = threadId
        self.branchKind = branchKind
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

    /// Source-compatibility convenience while fixtures and the legacy API
    /// still expose integer identities. Callers still observe `id` as String.
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
        mediaRefs: [SessionEventMediaRef] = [],
        cursor: String? = nil,
        orderTimeUs: Int64? = nil,
        threadId: String? = nil,
        branchKind: String? = nil
    ) {
        self.init(
            id: String(id),
            role: role,
            contentText: contentText,
            toolName: toolName,
            toolInputJSON: toolInputJSON,
            toolOutputText: toolOutputText,
            toolCallId: toolCallId,
            toolCallState: toolCallState,
            timestamp: timestamp,
            inActiveContext: inActiveContext,
            isHeadBranch: isHeadBranch,
            inputOrigin: inputOrigin,
            eventOrigin: eventOrigin,
            mediaRefs: mediaRefs,
            cursor: cursor,
            orderTimeUs: orderTimeUs,
            threadId: threadId,
            branchKind: branchKind
        )
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        if let stringId = (try? container.decode(String.self, forKey: .id))
            ?? (try? container.decode(String.self, forKey: .eventId)) {
            id = stringId
        } else if let integerId = (try? container.decode(Int.self, forKey: .id))
            ?? (try? container.decode(Int.self, forKey: .eventId)) {
            id = String(integerId)
        } else {
            throw DecodingError.keyNotFound(
                CodingKeys.id,
                .init(codingPath: decoder.codingPath, debugDescription: "Expected id or event_id")
            )
        }
        cursor = try container.decodeIfPresent(String.self, forKey: .cursor)
        orderTimeUs = try container.decodeIfPresent(Int64.self, forKey: .orderTimeUs)
        threadId = try container.decodeIfPresent(String.self, forKey: .threadId)
        branchKind = try container.decodeIfPresent(String.self, forKey: .branchKind)
        role = try container.decode(String.self, forKey: .role)
        contentText = try container.decodeIfPresent(String.self, forKey: .contentText)
        toolName = try container.decodeIfPresent(String.self, forKey: .toolName)
        toolInputJSON = try container.decodeIfPresent([String: JSONValue].self, forKey: .toolInputJSON)
        toolOutputText = try container.decodeIfPresent(String.self, forKey: .toolOutputText)
        toolCallId = try container.decodeIfPresent(String.self, forKey: .toolCallId)
        toolCallState = try container.decodeIfPresent(ToolCallState.self, forKey: .toolCallState)
        timestamp = try container.decode(String.self, forKey: .timestamp)
        inActiveContext = try container.decodeIfPresent(Bool.self, forKey: .inActiveContext)
            ?? (branchKind == nil || branchKind == "head")
        isHeadBranch = try container.decodeIfPresent(Bool.self, forKey: .isHeadBranch)
            ?? (branchKind == nil || branchKind == "head")
        inputOrigin = try container.decodeIfPresent(SessionInputOrigin.self, forKey: .inputOrigin)
        eventOrigin = try container.decodeIfPresent(String.self, forKey: .eventOrigin)
        mediaRefs = try container.decodeIfPresent([SessionEventMediaRef].self, forKey: .mediaRefs) ?? []
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encodeIfPresent(cursor, forKey: .cursor)
        try container.encodeIfPresent(orderTimeUs, forKey: .orderTimeUs)
        try container.encodeIfPresent(threadId, forKey: .threadId)
        try container.encodeIfPresent(branchKind, forKey: .branchKind)
        try container.encode(role, forKey: .role)
        try container.encodeIfPresent(contentText, forKey: .contentText)
        try container.encodeIfPresent(toolName, forKey: .toolName)
        try container.encodeIfPresent(toolInputJSON, forKey: .toolInputJSON)
        try container.encodeIfPresent(toolOutputText, forKey: .toolOutputText)
        try container.encodeIfPresent(toolCallId, forKey: .toolCallId)
        try container.encodeIfPresent(toolCallState, forKey: .toolCallState)
        try container.encode(timestamp, forKey: .timestamp)
        try container.encode(inActiveContext, forKey: .inActiveContext)
        try container.encode(isHeadBranch, forKey: .isHeadBranch)
        try container.encodeIfPresent(inputOrigin, forKey: .inputOrigin)
        try container.encodeIfPresent(eventOrigin, forKey: .eventOrigin)
        try container.encode(mediaRefs, forKey: .mediaRefs)
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case eventId
        case cursor
        case orderTimeUs
        case threadId
        case branchKind
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

    var legacyNumericId: Int? { Int(id) }

    var isSynthetic: Bool {
        id.hasPrefix("synthetic:") || (legacyNumericId.map { $0 < 0 } ?? false)
    }

    /// Compare transcript chronology without treating opaque identity or
    /// cursor bytes as sortable. Returns nil if the server has not supplied
    /// enough ordering information and timestamps are equal/unparseable.
    func isOrdered(before other: SessionEvent) -> Bool? {
        if let lhs = orderTimeUs, let rhs = other.orderTimeUs, lhs != rhs {
            return lhs < rhs
        }
        if let lhs = LonghouseDateParser.parse(timestamp),
           let rhs = LonghouseDateParser.parse(other.timestamp),
           lhs != rhs {
            return lhs < rhs
        }
        if let lhs = legacyNumericId, let rhs = other.legacyNumericId, lhs != rhs {
            return lhs < rhs
        }
        return nil
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
