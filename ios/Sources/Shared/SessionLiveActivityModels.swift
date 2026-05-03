import ActivityKit
import Foundation

struct SessionWatchAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable, Sendable {
        let presenceState: String
        let displayPhase: String
        let activeTool: String?
        let updatedAt: Int
        let isAttention: Bool
    }

    let sessionId: String
    let title: String
    let provider: String
    let project: String?
}

extension SessionDetail {
    func liveActivityContentState(updatedAt: Date = Date()) -> SessionWatchAttributes.ContentState {
        let state = runtimeDisplay?.state ?? presenceState ?? status ?? "unknown"
        let tool = RuntimeDisplayText.canonicalToolLabel(
            runtimeDisplay?.compactToolLabel ?? activeTool ?? presenceTool
        )
        let phase = RuntimeDisplayText.canonicalDisplayText(runtimeDisplay?.phaseLabel)
            ?? RuntimeDisplayText.canonicalDisplayText(displayPhase)
            ?? liveActivityPhaseLabel(state: state, tool: tool)
        return SessionWatchAttributes.ContentState(
            presenceState: state,
            displayPhase: phase,
            activeTool: tool,
            updatedAt: Int(updatedAt.timeIntervalSince1970),
            isAttention: state == "blocked"
        )
    }

    var liveActivityAttributes: SessionWatchAttributes {
        SessionWatchAttributes(
            sessionId: id,
            title: displayTitle,
            provider: provider,
            project: project
        )
    }

    private func liveActivityPhaseLabel(state: String, tool: String?) -> String {
        switch state {
        case "running":
            return tool.map { "Running \($0)" } ?? "Running"
        case "thinking":
            return "Thinking"
        case "needs_user":
            return "Ready"
        case "blocked":
            return tool.map { "Blocked on \($0)" } ?? "Needs permission"
        case "idle":
            return "Idle"
        default:
            return status == "completed" ? "Completed" : "Recent progress"
        }
    }
}
