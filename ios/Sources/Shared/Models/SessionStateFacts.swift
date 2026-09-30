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

extension SessionStateFacts {
    /// Is the served activity evidence still inside its window?
    ///
    /// Mirrors `web/src/shared/session/activityEvidence.ts`. Expired evidence becomes
    /// unknown, never idle or finished: absence of evidence is not evidence of
    /// an ending. A missing or unparseable window is not an expired one --
    /// inventing an expiry would hide live activity.
    func activityEvidenceIsLive(asOf now: Date = Date()) -> Bool {
        guard let validUntil = activityValidUntil, !validUntil.isEmpty else { return true }
        guard let expiresAt = LonghouseDateParser.parse(validUntil) else { return true }
        return now <= expiresAt
    }

    /// The first clock boundary that can retire the presentation's work claim.
    var workClaimValidUntil: String? {
        if pendingInteractionKind != nil || primary?.key == "needs_answer" || primary?.key == "needs_approval" {
            return nil
        }
        guard primary?.key == "delegated_work" else {
            return activityState == "thinking" || activityState == "executing" || activityState == "stalled"
                ? activityValidUntil : nil
        }
        guard let delegation, delegation.state == "pending", (delegation.count ?? 0) > 0 else { return nil }
        let delegationWindow = delegation.validUntil
        guard activityState == "thinking" || activityState == "executing" else {
            return delegationWindow
        }
        guard let activityDeadline = activityValidUntil.flatMap(LonghouseDateParser.parse) else {
            return delegationWindow
        }
        guard let delegationDeadline = delegationWindow.flatMap(LonghouseDateParser.parse) else {
            return activityValidUntil
        }
        return activityDeadline < delegationDeadline ? activityValidUntil : delegationWindow
    }

    /// The presentation's work claim owns its clock; delegation outlives parent activity.
    func workClaimExpired(asOf now: Date = Date()) -> Bool {
        if pendingInteractionKind != nil || primary?.key == "needs_answer" || primary?.key == "needs_approval" {
            return false
        }
        switch activityState {
        case "thinking", "executing":
            if !activityEvidenceIsLive(asOf: now) { return true }
        default:
            break
        }
        if primary?.key == "delegated_work" {
            guard let delegation, delegation.state == "pending", (delegation.count ?? 0) > 0 else {
                return true
            }
            return !delegation.isValid(asOf: now)
        }
        switch activityState {
        case "thinking", "executing", "stalled":
            return !activityEvidenceIsLive(asOf: now)
        default:
            return false
        }
    }
}
/// Connection state belongs to the viewer's workspace stream, not the provider.
/// A connected stream only means updates can arrive; it is never provider
/// liveness or host reachability evidence.
enum SessionRealtimeConnection: Equatable, Sendable {
    case connecting
    case connected
    case disconnected
}

/// Semantic state used by the native Ledger renderer. Provider activity and
/// expiry remain server-owned; the stream state only qualifies what the viewer
/// can currently know.
enum SessionLedgerEvidence: Equatable, Sendable {
    case working
    case attention
    case quiet
    case uncertain
}

/// Identity of one canonical provider activity observation. Transport frames,
/// heartbeats and control leases deliberately do not participate in this
/// identity: recovery needs a newer provider fact, not merely a live socket.
struct SessionProviderEvidenceIdentity: Equatable, Sendable {
    let observedAt: String
    let state: String
    let tool: String?
    let source: String?
}
/// One named unit of provider-reported background work. The provider owns the
/// vocabulary and lifecycle; the client deliberately keeps status raw instead
/// of translating it into a guessed "running" or "done" state.
struct SessionDelegationTask: Identifiable, Hashable, Codable, Sendable {
    let id: String
    let kind: String
    let status: String
    let description: String?
    let firstObservedAt: String?
    let startedAt: String?
    let lastActivityAt: String?
    /// The exact child session, when the provider and catalog established one.
    /// A missing link is not inferred from the task id.
    let sessionId: String?
    /// Nullable archive enrichment; absence never implies zero work.
    var userMessages: Int? = nil
    var assistantMessages: Int? = nil
    var toolCalls: Int? = nil
}

/// Provider-owned evidence for delegated/background work. `items == nil`
/// means the observation is aggregate-only; an empty array is authoritative
/// named emptiness and must not be treated as missing evidence.
struct SessionDelegationFacts: Hashable, Codable, Sendable {
    let state: String
    let count: Int?
    let kinds: [String: Int]?
    let source: String?
    let observedAt: String?
    let validUntil: String?
    let items: [SessionDelegationTask]?
}

/// Stable presentation buckets for the provider's canonical task kinds.
/// Unknown provider kinds stay visible under Other rather than being inferred
/// from a substring.
enum SessionDelegationCategory: String, CaseIterable, Hashable, Sendable {
    case agents
    case commands
    case monitors
    case other

    init(kind: String) {
        switch kind {
        case "subagent": self = .agents
        case "shell": self = .commands
        case "monitor": self = .monitors
        default: self = .other
        }
    }

    var title: String {
        switch self {
        case .agents: return "Agents"
        case .commands: return "Commands"
        case .monitors: return "Monitors"
        case .other: return "Other background work"
        }
    }

    func countLabel(_ count: Int) -> String {
        let noun: String
        switch self {
        case .agents: noun = count == 1 ? "agent" : "agents"
        case .commands: noun = count == 1 ? "command" : "commands"
        case .monitors: noun = count == 1 ? "monitor" : "monitors"
        case .other: noun = count == 1 ? "task" : "tasks"
        }
        return "\(count) \(noun)"
    }
}


extension SessionDelegationFacts {
    /// The server's TTL is a local clock boundary. No network event is needed
    /// before an otherwise live snapshot becomes unknown on this device.
    func isValid(asOf now: Date = Date()) -> Bool {
        guard let validUntil, let deadline = LonghouseDateParser.parse(validUntil) else {
            return true
        }
        return now < deadline
    }
}


extension SessionStateFacts {
    var providerEvidenceIdentity: SessionProviderEvidenceIdentity? {
        guard let observedAt = activityObservedAt,
              !observedAt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return nil
        }
        return SessionProviderEvidenceIdentity(
            observedAt: observedAt,
            state: activityState,
            tool: activityTool,
            source: activitySource
        )
    }
}

extension SessionStateFacts {
    /// The ledger verdict for the presentation's independently clocked work claim.
    ///
    /// The viewer's socket is deliberately not an input. A connected stream only
    /// means updates can arrive, so a `connecting` or `disconnected` socket is
    /// not evidence that the provider stopped: it belongs on the connection
    /// line. `valid_until` is what bounds a work claim, and the reader's clock
    /// is what retires it -- an expired window becomes `uncertain` (neutral,
    /// never idle and never ended), a live one keeps its claim.
    func ledgerEvidence(
        hasPendingInteraction: Bool = false,
        asOf now: Date = Date()
    ) -> SessionLedgerEvidence {
        if hasPendingInteraction || pendingInteractionKind != nil
            || primary?.key == "needs_answer" || primary?.key == "needs_approval" {
            return .attention
        }
        if workClaimExpired(asOf: now) { return .uncertain }
        let tone = primary?.tone
        if tone == "active" || tone == "running" || tone == "thinking"
            || activityState == "thinking" || activityState == "executing" {
            return .working
        }
        return .quiet
    }
}




struct SessionStateFacts: Hashable, Codable, Sendable {
    let contractVersion: Int
    let presentationPolicyVersion: Int
    let mode: String
    let dispositionState: String
    let dispositionCloseReason: String?
    let launchState: String?
    let runLifecycle: String?
    /// Provider-owned run identity. It fences optional delegation retention
    /// so a new run cannot inherit tasks from an earlier observation.
    var runId: String? = nil
    let activityState: String
    let activityRawKind: String?
    let activityTool: String?
    let activitySource: String?
    let activityObservedAt: String?
    let activityValidUntil: String?
    let controlOwnership: String
    let controlConnection: String
    /// Which timeline tier this session belongs to, decided server-side.
    /// "open" is what the user would say they have going right now.
    let workingSet: String
    /// Console unread acknowledgement, derived server-side: a Console turn
    /// settled and no human has opened the session since. Overlay on the
    /// tiers, not a tier (console-unread-acknowledgement spec).
    let unread: Bool
    let lastResultAt: String?
    let lastResultOutcome: String?
    let startTurn: SessionStateAction?
    let sendInput: SessionStateAction
    let interrupt: SessionStateAction
    let terminate: SessionStateAction
    let reattach: SessionStateAction
    let resume: SessionStateAction
    /// Whether this session can be branched into a new one that continues its
    /// conversation. Strictly narrower than `resume`, and computed by the
    /// server so the phone and the endpoint cannot disagree about what is
    /// offerable.
    let branch: SessionStateAction
    let pendingInteractionKind: String?
    let transcriptConvergence: String
    let primary: SessionStateLabel?
    let access: SessionStateLabel?
    let transcript: SessionStateLabel?
    /// Monotonic catalog commit coordinate for state-render settlement.
    let commitSeq: Int?
    /// Named background work, when the provider exposed this family on the
    /// same observation. Missing means this server/cached payload predates the
    /// family; it is not an empty registry.
    var delegation: SessionDelegationFacts? = nil

    static let unknown = SessionStateFacts(
        contractVersion: 1,
        presentationPolicyVersion: 1,
        mode: "unknown",
        dispositionState: "unknown",
        dispositionCloseReason: nil,
        launchState: nil,
        runLifecycle: nil,
        activityState: "unknown",
        activityRawKind: nil,
        activityTool: nil,
        activitySource: nil,
        activityObservedAt: nil,
        activityValidUntil: nil,
        controlOwnership: "unowned",
        controlConnection: "unknown",
        workingSet: "history",
        unread: false,
        lastResultAt: nil,
        lastResultOutcome: nil,
        startTurn: SessionStateAction(state: "unknown", reason: "missing_state_facts"),
        sendInput: SessionStateAction(state: "unknown", reason: "missing_state_facts"),
        interrupt: SessionStateAction(state: "unknown", reason: "missing_state_facts"),
        terminate: SessionStateAction(state: "unknown", reason: "missing_state_facts"),
        reattach: SessionStateAction(state: "unknown", reason: "missing_state_facts"),
        resume: SessionStateAction(state: "unknown", reason: "missing_state_facts"),
        branch: SessionStateAction(state: "unknown", reason: "missing_state_facts"),
        pendingInteractionKind: nil,
        transcriptConvergence: "unknown",
        primary: nil,
        access: nil,
        transcript: nil,
        commitSeq: nil
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
