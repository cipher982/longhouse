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

extension SessionWatchAttributes.ContentState {
    /// The served activity state this update carries, in one vocabulary.
    ///
    /// The app writes `stateFacts.activityState` as is. The server's Live
    /// Activity push (apns_sender) renames two of those states to presence
    /// words, "running" for executing and "idle" for quiescent, and older
    /// pushes also sent "needs_user". Both arrive in `presenceState`, so the
    /// aliases fold back here before anything maps them.
    var activityState: String {
        switch presenceState {
        case "running": return "executing"
        case "idle", "needs_user": return "quiescent"
        default: return presenceState
        }
    }

    /// The same signal the app's timeline row shows for this state: a pending
    /// interaction is attention, otherwise the activity state decides.
    var signal: TimelineSignal {
        isAttention ? .attention : TimelineSignal.forActivityState(activityState)
    }

    /// A word short enough for the Dynamic Island's compact trailing slot.
    var compactStateLabel: String {
        if isAttention { return "Needs you" }
        switch activityState {
        case "thinking": return "Think"
        case "executing": return "Run"
        case "blocked": return "Hold"
        case "stalled": return "Stall"
        case "quiescent": return "Idle"
        default: return "Unknown"
        }
    }
}

extension SessionDetail {
    func liveActivityContentState(updatedAt: Date = Date()) -> SessionWatchAttributes.ContentState {
        SessionWatchAttributes.ContentState(
            presenceState: stateFacts.activityState,
            displayPhase: stateFacts.primary?.label ?? "",
            activeTool: stateFacts.activityTool,
            updatedAt: Int(updatedAt.timeIntervalSince1970),
            isAttention: stateFacts.hasAnswerablePendingInteraction
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
}
