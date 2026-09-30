import Foundation
import SwiftUI

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
    /// When this session began. Frozen for the life of the session, which is
    /// what the timeline's display order uses.
    let startedAt: String?
    let summary: String?
    let summaryStatus: String?
    let firstUserMessage: String?
    /// Canonical machine identifier from the session record.
    let deviceId: String?
    /// Server-projected archive-search context. Nil on ordinary timeline rows.
    let matchSnippet: String?
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
        startedAt: String? = nil,
        summary: String? = nil,
        summaryStatus: String? = nil,
        firstUserMessage: String? = nil,
        deviceId: String? = nil,
        matchSnippet: String? = nil,
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
        self.startedAt = startedAt
        self.summary = summary
        self.summaryStatus = summaryStatus
        self.firstUserMessage = firstUserMessage
        self.deviceId = deviceId
        self.matchSnippet = matchSnippet
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
        self.runtimeDisplay = runtimeDisplay
        self.timelineCard = timelineCard
        self.stateFacts = stateFacts
    }

    var isClosed: Bool { stateFacts.dispositionState == "closed" }

    /// Is this session part of what the user is currently carrying?
    ///
    /// Reads the server's `working_set` tier, never the disposition: a closed
    /// session is never open, and an idle session with no terminal is history
    /// even though its disposition still reads open. Server-side, so the phone
    /// and the page cut cannot disagree about what "open" means.
    var isOpen: Bool { stateFacts.workingSet == "open" }

    var isBlocked: Bool { isBlocked(asOf: Date()) }
    var isUserActive: Bool { userState == nil || userState == "active" }
    var needsAttention: Bool {
        if isClosed || !isUserActive { return false }
        return stateFacts.pendingInteractionKind != nil
    }
    var isExecuting: Bool { isExecuting(asOf: Date()) }

    /// Activity, judged against the window the server stamped on the evidence.
    ///
    /// Without this a view holding a snapshot renders whatever it last received
    /// for as long as it stays on screen. A wedged turn sends no further frame,
    /// so a correct server and a "Working" bar coexist indefinitely.
    func isExecuting(asOf now: Date) -> Bool {
        guard !isClosed, stateFacts.activityEvidenceIsLive(asOf: now) else { return false }
        return ["thinking", "executing"].contains(stateFacts.activityState)
    }

    func isBlocked(asOf now: Date) -> Bool {
        guard !isClosed, stateFacts.activityEvidenceIsLive(asOf: now) else { return false }
        return stateFacts.activityState == "blocked"
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

    /// The session's project, or `nil` when Longhouse never resolved one.
    ///
    /// Absence is rendered by omitting the label, not by inventing one. A
    /// session whose provider keeps its working directory outside the
    /// transcript has no project attribution at all, and "Unknown project"
    /// asserted a fact the timeline does not have.
    var projectLabel: String? {
        guard let project else { return nil }
        let trimmed = project.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    var timelineMachineLabel: String? {
        for candidate in [deviceId, headOriginLabel, homeLabel] {
            guard let label = candidate?.trimmingCharacters(in: .whitespacesAndNewlines), !label.isEmpty else {
                continue
            }
            return label
        }
        return nil
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

    /// Cap a resident timeline list to `limit` rows without cutting the shelf.
    ///
    /// The limit belongs to history. The server admits every open session by
    /// predicate, so dropping whichever row has the oldest anchor drops exactly
    /// the quiet-but-open session that admission exists to protect: a Helm
    /// session with a terminal attached and no transcript write for a day.
    static func residentCap(_ sessions: [SessionSummary], limit: Int) -> [SessionSummary] {
        guard sessions.count > limit else { return sessions }
        let open = sessions.filter(\.isOpen)
        let history = sessions.filter { !$0.isOpen }
        return open + history.prefix(max(0, limit - open.count))
    }
}


extension SessionSummary {
    /// What VoiceOver says for the row's status: the server's primary label,
    /// verbatim, the same words the row shows. The client adds nothing but the
    /// freshness gate: a work claim whose evidence window has passed on the
    /// reader's clock is spoken as "Activity uncertain", never as the cached
    /// "Using Bash" (the ledger's `.uncertain` verdict).
    func spokenStatusLabel(asOf now: Date = Date()) -> String {
        if !isClosed, stateFacts.workClaimExpired(asOf: now) {
            return "Activity uncertain"
        }
        let label = timelineStatusLabel
        return label.isEmpty ? "Activity unknown" : label
    }
}


/// The wire shape is the whole model: these carry no default, no derived
/// value, and no behavior the screens do not read straight off the response.
typealias TimelineBadgePresentation = APITimelineBadgePresentationResponse
typealias TimelineStatusPresentation = APITimelineStatusPresentationResponse

struct TimelineCardPresentation: Codable, Hashable, Sendable {
    let ownership: TimelineBadgePresentation
    let status: TimelineStatusPresentation
    let borderTone: String
}
