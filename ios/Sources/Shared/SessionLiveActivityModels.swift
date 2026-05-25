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
        let state: String
        let tool: String?
        let phase: String?
        if let runtimeDisplay {
            state = runtimeDisplay.state ?? "unknown"
            tool = RuntimeDisplayText.canonicalToolLabel(runtimeDisplay.compactToolLabel)
            phase = RuntimeDisplayText.canonicalDisplayText(Optional(runtimeDisplay.phaseLabel))
        } else {
            state = presenceState ?? status ?? "unknown"
            tool = RuntimeDisplayText.canonicalToolLabel(activeTool ?? presenceTool)
            phase = RuntimeDisplayText.canonicalDisplayText(displayPhase)
        }
        return SessionWatchAttributes.ContentState(
            presenceState: state,
            displayPhase: phase ?? liveActivityPhaseLabel(state: state, tool: tool),
            activeTool: tool,
            updatedAt: Int(updatedAt.timeIntervalSince1970),
            isAttention: runtimeDisplay?.needsAttention ?? (state == "blocked")
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
            return status == "completed" ? "Closed" : "Unknown"
        }
    }
}
