import Combine
import Foundation

/// One frame received on the session workspace stream, kept only as long as
/// the activity strip can draw it. Nothing here is durable or replayed: the
/// strip is a twelve-second window onto what the phone actually received,
/// which is the whole point — a wedged turn flattens, a busy one does not.
struct ActivityPulse: Equatable, Sendable {
    enum Kind: Equatable, Sendable {
        /// A tool call opened (`tool_call_state == running`).
        case toolStart
        /// A tool call settled (completed / failed / cancelled).
        case toolResult
        /// A durable assistant or user row landed.
        case message
        /// Provisional text grew — the Codex bridge ships these per delta.
        case textDelta
        /// Runtime/catalog change with no transcript preview attached.
        case state

        /// Bar height as a fraction of the strip. Boundaries are tall, deltas
        /// short, so a Codex burst reads as a dense low band with tall posts at
        /// the tool edges rather than a wall.
        var height: Double {
            switch self {
            case .toolStart: return 1.0
            case .message: return 0.9
            case .toolResult: return 0.66
            case .textDelta: return 0.34
            case .state: return 0.2
            }
        }
    }

    let at: Date
    let kind: Kind
}

@MainActor
final class ActivityPulseStore: ObservableObject {
    /// Visible history. Bars older than this have drifted off the left edge.
    static let window: TimeInterval = 12
    /// A Codex burst can exceed 10 frames/s; keep the array bounded regardless
    /// of how the window prunes.
    private static let maxPulses = 256

    @Published private(set) var pulses: [ActivityPulse] = []

    func record(_ kind: ActivityPulse.Kind, at now: Date = Date()) {
        pulses.append(ActivityPulse(at: now, kind: kind))
        let cutoff = now.addingTimeInterval(-(Self.window + 1))
        if let firstLive = pulses.firstIndex(where: { $0.at >= cutoff }) {
            if firstLive > 0 { pulses.removeFirst(firstLive) }
        } else {
            pulses.removeAll()
        }
        if pulses.count > Self.maxPulses {
            pulses.removeFirst(pulses.count - Self.maxPulses)
        }
    }

    func reset() {
        pulses.removeAll()
    }

    /// Classify a `workspace_changed` frame by what it carried. The preview
    /// is the only content the frame has; a frame without one is a runtime
    /// or catalog update.
    nonisolated static func classify(
        toolName: String?,
        toolCallState: String?,
        isProvisional: Bool?
    ) -> ActivityPulse.Kind {
        if let toolName, !toolName.isEmpty {
            return toolCallState == "running" ? .toolStart : .toolResult
        }
        if isProvisional == true {
            return .textDelta
        }
        if isProvisional == false {
            return .message
        }
        return .state
    }

    /// A frame without a preview still says what woke it. Durable ingest is
    /// how a Claude turn shows its tool boundaries (it never streams text), so
    /// it draws as a result bar; a bare runtime wake is a state tick; a read
    /// or title update is bookkeeping and draws nothing.
    nonisolated static func classify(_ change: SessionWorkspaceStream.WorkspaceChanged) -> ActivityPulse.Kind? {
        if let preview = change.transcript_preview {
            return classify(
                toolName: preview.tool_name,
                toolCallState: preview.tool_call_state,
                isProvisional: preview.is_provisional
            )
        }
        return classify(changeKind: change.change_kind)
    }

    nonisolated static func classify(changeKind: String?) -> ActivityPulse.Kind? {
        switch changeKind {
        case "ingest": return .toolResult
        case "transcript_preview": return .textDelta
        case "read_update", "title_update": return nil
        default: return .state
        }
    }
}
