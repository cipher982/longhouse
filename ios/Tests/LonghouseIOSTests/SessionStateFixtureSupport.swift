import Foundation
@testable import Longhouse

func makeSessionStateFacts(
    activity: String = "unknown",
    owned: Bool = true,
    pendingInteractionKind: String? = nil,
    launchState: String? = nil,
    closed: Bool = false,
    tool: String? = nil
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
        mode: owned ? "helm" : "shadow",
        dispositionState: closed ? "closed" : "open",
        launchState: launchState,
        runLifecycle: closed ? "ended" : "running",
        activityState: activity,
        activityTool: tool,
        activityObservedAt: nil,
        activityValidUntil: nil,
        controlOwnership: owned ? "owned" : "unowned",
        controlConnection: owned ? "connected" : "not_applicable",
        sendInput: owned ? available : unavailable,
        interrupt: owned ? available : unavailable,
        terminate: owned ? available : unavailable,
        reattach: unavailable,
        resume: unavailable,
        pendingInteractionKind: pendingInteractionKind,
        transcriptConvergence: "current",
        primary: primary,
        access: SessionStateLabel(
            key: owned ? "live_control" : "search_only",
            label: owned ? "Live control" : "Search only",
            tone: owned ? "live" : "search",
            observedAt: nil
        ),
        transcript: nil
    )
}

extension JSONDecoder {
    /// Migrates pre-session-state inline fixtures without adding a production
    /// fallback. The synthesized facts are deliberately canonical and ignore
    /// legacy headline/detail copy.
    func decodeSessionFixture<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        try decode(type, from: try sessionStateFixtureData(data))
    }
}

func sessionStateFixtureData(_ data: Data) throws -> Data {
    let object = try JSONSerialization.jsonObject(with: data)
    let migrated = migrateSessionFixtureNode(object)
    return try JSONSerialization.data(withJSONObject: migrated)
}

private func migrateSessionFixtureNode(_ value: Any) -> Any {
    if let rows = value as? [Any] {
        return rows.map(migrateSessionFixtureNode)
    }
    guard var object = value as? [String: Any] else { return value }
    for (key, child) in object {
        object[key] = migrateSessionFixtureNode(child)
    }
    guard let runtime = object["runtime_display"] as? [String: Any] else { return object }
    let facts = canonicalFixtureFacts(runtime: runtime, capabilities: object["capabilities"] as? [String: Any])
    if object["state_facts"] == nil {
        object["state_facts"] = facts.domain
    }
    if object["session_state"] == nil {
        object["session_state"] = facts.api
    }
    return object
}

private func canonicalFixtureFacts(
    runtime: [String: Any],
    capabilities: [String: Any]?
) -> (domain: [String: Any], api: [String: Any]) {
    let rawState = runtime["state"] as? String
    let pause = runtime["pause_request"] as? [String: Any]
    let pendingKind: String? = pause?["status"] as? String == "pending"
        ? ((pause?["kind"] as? String) == "structured_question" ? "question" : "approval")
        : nil
    let tool = runtime["compact_tool_label"] as? String
    let activity: String
    switch rawState {
    case "running": activity = "executing"
    case "thinking": activity = "thinking"
    case "idle", "needs_user": activity = "quiescent"
    case "blocked": activity = "blocked"
    case "stalled": activity = "stalled"
    default: activity = "unknown"
    }
    let closed = runtime["lifecycle"] as? String == "closed"
    let owned = runtime["control_path"] as? String == "managed"
    let live = capabilities?["live_control_available"] as? Bool == true
    let reattach = capabilities?["host_reattach_available"] as? Bool == true
    let canReply = capabilities?["reply_to_live_session_available"] as? Bool == true
    let action = { (available: Bool) -> [String: Any] in
        available ? ["state": "available"] : ["state": "unavailable", "reason": "fixture_not_granted"]
    }
    let primary: [String: Any]
    if closed {
        primary = ["key": "closed", "label": "Closed", "tone": "closed"]
    } else if pendingKind != nil {
        primary = ["key": "needs_answer", "label": "Needs answer", "tone": "blocked"]
    } else {
        switch activity {
        case "executing":
            primary = ["key": "executing", "label": tool.map { "Using \($0)" } ?? "Running", "tone": "running"]
        case "thinking": primary = ["key": "thinking", "label": "Thinking", "tone": "thinking"]
        case "quiescent": primary = ["key": "idle", "label": "Idle", "tone": "idle"]
        case "blocked": primary = ["key": "blocked", "label": "Blocked", "tone": "blocked"]
        case "stalled": primary = ["key": "stalled", "label": "Stalled", "tone": "stalled"]
        default: primary = ["key": "activity_unknown", "label": "Activity unknown", "tone": "quiet"]
        }
    }
    let access: [String: Any]
    if owned && live {
        access = ["key": "live_control", "label": "Live control", "tone": "live"]
    } else if owned && reattach {
        access = ["key": "reattach", "label": "Reattach", "tone": "reattach"]
    } else if owned {
        access = ["key": "control_unknown", "label": "Control unknown", "tone": "inactive"]
    } else {
        access = ["key": "search_only", "label": "Search only", "tone": "search"]
    }
    let actions = [
        "send_input": action(live && canReply),
        "interrupt": action(live),
        "terminate": action(live),
        "reattach": action(!live && reattach),
        "resume": action(reattach),
    ]
    var activityFacts: [String: Any] = ["state": activity]
    if let tool { activityFacts["tool"] = tool }
    var api: [String: Any] = [
        "state_contract_version": 1,
        "presentation_policy_version": 1,
        "mode": owned ? "helm" : "shadow",
        "disposition": ["state": closed ? "closed" : "open"],
        "run": ["lifecycle": closed ? "ended" : (activity == "unknown" ? "unknown" : "running")],
        "activity": activityFacts,
        "control": [
            "ownership": owned ? "owned" : "unowned",
            "connection": owned ? (live ? "connected" : reattach ? "disconnected" : "unknown") : "not_applicable",
            "actions": actions,
        ],
        "transcript": ["convergence": "current", "searchable": true, "live_observation": false],
        "host": ["state": runtime["host_state"] as? String ?? "unknown"],
        "presentation": ["primary": primary, "access": access],
    ]
    if runtime["headline"] as? String == "Launching" {
        api["launch"] = ["state": "pending"]
    }
    if let pendingKind, let pause {
        api["pending_interaction"] = [
            "id": pause["id"] as? String ?? "fixture-interaction",
            "kind": pendingKind,
            "can_respond": pause["can_respond"] as? Bool ?? false,
        ]
    }
    var domain: [String: Any] = [
        "contract_version": 1,
        "presentation_policy_version": 1,
        "mode": owned ? "helm" : "shadow",
        "disposition_state": closed ? "closed" : "open",
        "launch_state": runtime["headline"] as? String == "Launching" ? "pending" : NSNull(),
        "run_lifecycle": closed ? "ended" : (activity == "unknown" ? "unknown" : "running"),
        "activity_state": activity,
        "control_ownership": owned ? "owned" : "unowned",
        "control_connection": owned ? (live ? "connected" : reattach ? "disconnected" : "unknown") : "not_applicable",
        "send_input": actions["send_input"]!,
        "interrupt": actions["interrupt"]!,
        "terminate": actions["terminate"]!,
        "reattach": actions["reattach"]!,
        "resume": actions["resume"]!,
        "transcript_convergence": "current",
        "primary": primary,
        "access": access,
    ]
    if let pendingKind { domain["pending_interaction_kind"] = pendingKind }
    if let tool { domain["activity_tool"] = tool }
    return (domain, api)
}
