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
        SessionWatchAttributes.ContentState(
            presenceState: stateFacts?.activityState ?? "unknown",
            displayPhase: stateFacts?.primary?.label ?? "",
            activeTool: stateFacts?.activityTool,
            updatedAt: Int(updatedAt.timeIntervalSince1970),
            isAttention: stateFacts?.pendingInteractionKind != nil
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
