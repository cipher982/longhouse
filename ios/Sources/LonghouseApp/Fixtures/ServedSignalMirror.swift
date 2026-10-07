#if DEBUG
import Foundation

extension SessionStateFacts {
    /// Fixture-only mirror of the server's `session_state_contract._signal`.
    ///
    /// Previews, UI-test fixtures and unit tests hand-build state facts; a real
    /// host always serves `presentation.signal`, so fixtures must too. This
    /// recomputes it from the headline the fixture chose. Never used by
    /// production code: the app reads the served value.
    func withMirroredSignal() -> SessionStateFacts {
        var copy = self
        copy.signal = Self.mirroredSignal(primary: primary, activityState: activityState,
                                          activityValidUntil: activityValidUntil,
                                          delegationValidUntil: delegation?.validUntil)
        return copy
    }

    static func mirroredSignal(
        primary: SessionStateLabel?,
        activityState: String,
        activityValidUntil: String?,
        delegationValidUntil: String?
    ) -> SessionStateSignal {
        guard let primary else { return SessionStateSignal(state: "unknown", validUntil: nil) }
        if primary.key == "closed" { return SessionStateSignal(state: "closed", validUntil: nil) }
        if primary.tone == "blocked" || primary.tone == "stalled" {
            return SessionStateSignal(state: "attention", validUntil: primary.key == "stalled" ? activityValidUntil : nil)
        }
        if ["running", "thinking", "active"].contains(primary.tone) {
            let validUntil: String?
            if primary.key == "delegated_work" {
                validUntil = delegationValidUntil
            } else if activityState == "thinking" || activityState == "executing" {
                validUntil = activityValidUntil
            } else {
                validUntil = nil
            }
            return SessionStateSignal(state: "working", validUntil: validUntil)
        }
        if ["idle", "ready", "ended"].contains(primary.key) {
            return SessionStateSignal(state: "quiet", validUntil: nil)
        }
        return SessionStateSignal(state: "unknown", validUntil: nil)
    }
}
#endif
