import Foundation
@testable import Longhouse

func makeSessionStateFacts(
    activity: String = "unknown",
    owned: Bool = true,
    mode: String? = nil,
    pendingInteractionKind: String? = nil,
    launchState: String? = nil,
    closed: Bool = false,
    tool: String? = nil,
    startTurnAvailable: Bool = false,
    sendInputAvailable: Bool? = nil,
    accessLabel: String? = nil
) -> SessionStateFacts {
    let available = SessionStateAction(state: "available", reason: nil)
    let unavailable = SessionStateAction(state: "unavailable", reason: "fixture_not_granted")
    let primary: SessionStateLabel = {
        if closed { return SessionStateLabel(key: "closed", label: "Closed", tone: "closed", observedAt: nil) }
        if pendingInteractionKind != nil {
            return SessionStateLabel(key: "needs_answer", label: "Needs answer", tone: "blocked", observedAt: nil)
        }
        switch activity {
        case "executing": return SessionStateLabel(key: "executing", label: tool.map { "Using \($0)" } ?? "Running", tone: "running", observedAt: nil)
        case "thinking": return SessionStateLabel(key: "thinking", label: "Thinking", tone: "thinking", observedAt: nil)
        case "quiescent": return SessionStateLabel(key: "idle", label: "Idle", tone: "idle", observedAt: nil)
        case "blocked": return SessionStateLabel(key: "blocked", label: "Blocked", tone: "blocked", observedAt: nil)
        case "stalled": return SessionStateLabel(key: "stalled", label: "Stalled", tone: "stalled", observedAt: nil)
        default: return SessionStateLabel(key: "activity_unknown", label: "Activity unknown", tone: "quiet", observedAt: nil)
        }
    }()
    return SessionStateFacts(
        contractVersion: 1,
        presentationPolicyVersion: 1,
        mode: mode ?? (owned ? "helm" : "shadow"),
        dispositionState: closed ? "closed" : "open",
        launchState: launchState,
        runLifecycle: closed ? "ended" : "running",
        activityState: activity,
        activityRawKind: nil,
        activityTool: tool,
        activitySource: nil,
        activityObservedAt: nil,
        activityValidUntil: nil,
        controlOwnership: owned ? "owned" : "unowned",
        controlConnection: owned ? "connected" : "not_applicable",
        startTurn: startTurnAvailable ? available : unavailable,
        sendInput: (sendInputAvailable ?? owned) ? available : unavailable,
        interrupt: owned ? available : unavailable,
        terminate: owned ? available : unavailable,
        reattach: unavailable,
        resume: unavailable,
        pendingInteractionKind: pendingInteractionKind,
        transcriptConvergence: "current",
        primary: primary,
        access: SessionStateLabel(
            key: accessLabel == nil ? (owned ? "live_control" : "search_only") : "control_unknown",
            label: accessLabel ?? (owned ? "Live control" : "Search only"),
            tone: accessLabel == nil ? (owned ? "live" : "search") : "quiet",
            observedAt: nil
        ),
        transcript: nil
    )
}

func addingSessionStateFacts(
    _ facts: SessionStateFacts,
    to data: Data,
    sessionKey: String? = nil
) throws -> Data {
    var root = try JSONSerialization.jsonObject(with: data) as! [String: Any]
    let encoder = JSONEncoder()
    encoder.keyEncodingStrategy = .convertToSnakeCase
    let factsObject = try JSONSerialization.jsonObject(with: encoder.encode(facts))
    if let sessionKey {
        var session = root[sessionKey] as! [String: Any]
        session["state_facts"] = factsObject
        root[sessionKey] = session
    } else {
        root["state_facts"] = factsObject
    }
    return try JSONSerialization.data(withJSONObject: root)
}

extension JSONDecoder {
    func decodeSessionFixture<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        try decode(type, from: data)
    }
}
