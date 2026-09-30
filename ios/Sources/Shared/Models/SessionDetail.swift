import Foundation
import SwiftUI

/// The subset of the server capability projection the app actually consults.
/// The response carries more fields; add one here when a screen reads it.
struct SessionCapabilities: Codable, Sendable {
    let canQueueNextInput: Bool?
    let canSteerActiveTurn: Bool?
    let defaultInputIntent: String?
    let composerPlaceholder: String?
    let attachImages: Bool?
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
    @DefaultUnknownSessionStateFacts var stateFacts: SessionStateFacts
    var transcriptPreview: SessionTranscriptPreview? = nil
    /// Registered machine id the session runs on; the header's host name.
    var deviceId: String? = nil
    /// Longhouse sends for this session, newest first, each carrying the durable
    /// event it became once ingest linked it. Clients resolve optimistic rows by
    /// this identity, whether or not that event is on the page they loaded.
    var inputReceipts: [SessionInputReceipt]? = nil
    /// The most recent turn the provider reported as finished.
    var lastTurn: SessionLastTurn? = nil
    /// The provider's latest away recap, when it wrote one.
    var recap: SessionRecap? = nil
    /// The model/context line from the provider's last turn-ending response.
    var usageLatest: SessionUsageLatest? = nil
    /// Selected model for new per-turn inputs. This is distinct from
    /// ``usageLatest``, which describes the provider's last completed turn.
    var selectedModel: String? = nil

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
        if stateFacts.mode == "console" {
            return stateFacts.startTurn?.isAvailable == true
        }
        return stateFacts.sendInput.isAvailable
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

    /// Image support is derived from the provider/mode capability table on
    /// the server and is only enabled when the current send action is ready.
    var attachImagesEnabled: Bool {
        canSendLive && (capabilities.attachImages ?? false)
    }

    var canQueueNextInput: Bool {
        capabilities.canQueueNextInput ?? false
    }

    var canSteerActiveTurn: Bool {
        capabilities.canSteerActiveTurn ?? false
    }

    /// Why Longhouse cannot send into this session right now, as an observation
    /// rather than one collapsed "offline" guess. Helm and Console block for
    /// different reasons, and only some of them are faults the user can act on:
    /// a finished Console run on a machine that advertises no turn adapter is
    /// ordinary, while an unreachable machine is an outage. Collapsing the two
    /// produced an orange "Control degraded / until the host reconnects" on a
    /// session whose host was connected the whole time.
    enum ControlBlock: Equatable {
        case none
        case closed
        case launching
        /// The machine running this session is unreachable.
        case machineOffline
        /// Helm holds a control lease and it is not answering.
        case controlUnhealthy
        /// Control evidence expired; Longhouse does not know.
        case controlUnknown
        /// Console: the machine is reachable but advertises no turn adapter.
        case noTurnPath
        /// Console: no machine + working directory recorded to run in.
        case noExecutionTarget
        /// Console: blocked for a reason this build does not have copy for.
        case consoleUnavailable
        /// Helm: not attached, but the control plane can be reattached.
        case reattachable
        /// Helm: the control path is closed and there is nothing to reattach.
        case controlClosed
        /// Owned, reachable, but this control path never accepts typed input.
        case readOnly
        /// Not owned by Longhouse at all.
        case imported
        /// Helm: the provider run finished. Not a fault — Resume is the step.
        case runEnded

        /// Only an outage earns the loud treatment. Everything else is a
        /// capability statement, not an alarm.
        var isFault: Bool {
            switch self {
            case .machineOffline, .controlUnhealthy, .controlUnknown:
                return true
            default:
                return false
            }
        }
    }

    var controlBlock: ControlBlock {
        if canSendLive { return .none }
        if isClosed { return .closed }
        if stateFacts.launchState == "pending" || stateFacts.launchState == "dispatched" { return .launching }
        if stateFacts.controlOwnership != "owned" { return .imported }
        if stateFacts.mode == "console" {
            switch stateFacts.startTurn?.reason {
            case "machine_offline": return .machineOffline
            case "adapter_unavailable": return .noTurnPath
            case "execution_target_missing": return .noExecutionTarget
            // Never .readOnly: Console has no typed-input path to be read-only
            // about, and the server declines to label an unrecognized blocker
            // rather than guessing. Match that instead of inventing a claim.
            default: return .consoleUnavailable
            }
        }
        // An ended Helm run is not a control fault. Ending the run clears the
        // durable run id, which by design rejects every run-bound control head,
        // so control reads owned/unknown and this fell through to
        // `.controlUnknown` — a fault, with the orange triangle — for the
        // ordinary act of exiting a terminal.
        //
        // Machine reachability wins: it decides whether Resume can run at all.
        // An ended run then outranks reattach, because reattach eligibility is
        // projected from a durable connection row that does not consult the run
        // — a stale row would otherwise offer "Reattach" for a run that is over.
        // This is the same order the server uses in
        // `_control_unavailable_sentence`.
        if ["offline", "stale"].contains(runtimeDisplay.hostState) { return .machineOffline }
        if stateFacts.mode == "helm" && stateFacts.runLifecycle == "ended" { return .runEnded }
        if stateFacts.reattach.isAvailable { return .reattachable }
        switch stateFacts.controlConnection {
        case "degraded": return .controlUnhealthy
        case "disconnected": return .controlClosed
        case "unknown": return .controlUnknown
        default: return .readOnly
        }
    }

    var isControlOffline: Bool { controlBlock.isFault }

    var isReadOnly: Bool {
        !canSendLive && !isControlOffline
    }

    var runtimePhaseState: String { stateFacts.activityState }

    var runtimePhaseLabel: String { stateFacts.primary?.label ?? "" }

    /// Nil rather than a placeholder, so copy can drop the word instead of
    /// rendering "new new turns".
    private var providerLabel: String? {
        let name = provider.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return nil }
        return name.prefix(1).uppercased() + name.dropFirst()
    }

    var controlHealthMessage: String? {
        switch controlBlock {
        case .none:
            return nil
        case .closed:
            return "This session is closed."
        case .launching:
            return "Session is still starting."
        case .machineOffline:
            return "The machine running this session is offline. Sending resumes when it reconnects."
        case .controlUnhealthy:
            return "Longhouse's control link to this session stopped answering."
        case .controlUnknown:
            return "Longhouse can't confirm the control link right now."
        case .noTurnPath:
            guard let providerLabel else { return "This session's machine isn't accepting new turns." }
            return "This session's machine isn't accepting new \(providerLabel) turns."
        case .noExecutionTarget:
            return "Longhouse has no machine and folder recorded to run this in."
        case .consoleUnavailable:
            return "Longhouse can't start a new turn on this session right now."
        case .reattachable:
            return "Longhouse isn't attached to this session. Reattach to steer it from here."
        case .controlClosed:
            return "Longhouse's control path to this session is closed."
        case .readOnly:
            return "This managed session is read-only."
        case .imported:
            return "Read-only imported session."
        case .runEnded:
            return stateFacts.resume.isAvailable
                ? "This session's run has ended. Resume it to keep going."
                : "This session's run has ended."
        }
    }

    var controlBlockIcon: String {
        switch controlBlock {
        case .machineOffline: return "wifi.slash"
        case .controlUnhealthy, .controlUnknown: return "exclamationmark.triangle"
        case .closed: return "archivebox"
        case .launching: return "hourglass"
        case .noTurnPath, .noExecutionTarget, .consoleUnavailable: return "nosign"
        case .reattachable: return "arrow.triangle.2.circlepath"
        case .runEnded: return "flag.checkered"
        case .controlClosed: return "bolt.slash"
        default: return "eye"
        }
    }

    /// Nil when the contract declined to emit an access label. The server drops
    /// it when the primary label already carries the whole story — a closed
    /// Console session, say — and falling back to "Read only" there put a chip
    /// beside "Closed" that claimed something the server deliberately refused
    /// to claim.
    var runtimeCapabilityLabel: String? {
        if let label = stateFacts.access?.label.trimmingCharacters(in: .whitespacesAndNewlines), !label.isEmpty {
            return label
        }
        if stateFacts.launchState == "pending" || stateFacts.launchState == "dispatched" {
            return "Launching"
        }
        if isControlOffline { return "Control offline" }
        return nil
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
            return "Message"
        }
        return placeholder
    }

    /// The served primary label, or the neutral unknown label when the server
    /// deliberately makes no runtime claim (a session with no run and no
    /// applicable launch/ready/interaction state). An empty headline rendered a
    /// blank status row, which is the one thing "unknown is information" cannot
    /// mean.
    var runtimeHeadline: String {
        let label = stateFacts.primary?.label.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return label.isEmpty ? "Activity unknown" : label
    }

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
        var copy = self
        copy.transcriptPreview = transcriptPreview
        return copy
    }

    /// Detail and tail endpoints share the session identity but may carry
    /// different optional enrichments. A newer response can update state
    /// without proving that an absent optional field means "delete" — retain
    /// same-run enrichment only; unknown run identity never resurrects it.
    func preservingOptionalEnrichment(from previous: SessionDetail) -> SessionDetail {
        var copy = self
        copy.transcriptPreview = transcriptPreview ?? previous.transcriptPreview
        copy.deviceId = deviceId ?? previous.deviceId
        copy.inputReceipts = inputReceipts ?? previous.inputReceipts
        copy.lastTurn = lastTurn ?? previous.lastTurn
        copy.recap = recap ?? previous.recap
        copy.usageLatest = usageLatest ?? previous.usageLatest
        copy.selectedModel = selectedModel ?? previous.selectedModel
        if stateFacts.delegation == nil,
           let runId = stateFacts.runId,
           previous.stateFacts.runId == runId {
            copy.stateFacts.delegation = previous.stateFacts.delegation
        }
        return copy
    }

    var withoutTranscriptPreview: SessionDetail {
        replacingTranscriptPreview(nil)
    }
}

extension SessionDetail {
    /// The session-level presentation verdict, plus a host observation
    /// that may retract a work claim whose owning window has not expired.
    ///
    /// A host we positively observed offline or stale outranks a work claim, but
    /// it may never *add* an alarm: an idle session stays idle rather than
    /// reading "Activity uncertain" because the laptop is asleep. The warm
    /// escalation for an unreachable machine belongs to the control/access
    /// surface, which is the one that owns the action.
    func ledgerEvidence(asOf now: Date = Date()) -> SessionLedgerEvidence {
        guard !isClosed else { return .quiet }
        let base = stateFacts.ledgerEvidence(
            hasPendingInteraction: activePauseRequest != nil,
            asOf: now
        )
        if base == .working, ["offline", "stale"].contains(runtimeDisplay.hostState) {
            return .uncertain
        }
        return base
    }
}


/// The provider's own catch-up note, written while the user was away.
struct SessionRecap: Codable, Hashable, Sendable {
    let text: String
    let at: String
}

/// The provider's last turn-ending response as the server words it:
/// "opus 5 · high · 501k ctx". Rendered verbatim so iOS and web cannot drift.
struct SessionUsageLatest: Codable, Hashable, Sendable {
    let label: String
}

/// The most recent turn the provider reported as finished, for the session chrome.
struct SessionLastTurn: Codable, Hashable, Sendable {
    let durationMs: Int
    let endedAt: String
    let eventId: String?
    var outcome: String? = nil
}
