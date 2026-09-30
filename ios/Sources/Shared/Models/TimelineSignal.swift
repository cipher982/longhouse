import Foundation
import SwiftUI

/// The single attention axis for a timeline row, shared by the app card and the
/// home-screen widget (and mirrored on web in lib/sessionRuntime.ts). Three
/// semantic stops the user can read pre-attentively, plus a closed/quiet rest:
///   - attention: the session is WAITING ON YOU - steady ember, never pulses.
///   - working:   the session is actively running - flame, breathing (live only).
///   - quiet:     idle/stale - grey, static.
///   - closed:    ended - dimmed grey, static.
/// Provider identity color stays on the glyph; it never bleeds into this axis.
enum TimelineSignal {
    case attention
    case working
    case quiet
    case unknown
    case closed

    /// Ember for "needs you", flame for live work, cooled ash at rest — the
    /// web's fire ramp. Ember and flame also differ in motion (only work
    /// breathes) and the status label text is the redundant code.
    static let attentionColor = Ember.ember
    static let workingColor = Ember.flame

    /// The leading dot color - the loudest at-a-glance signal.
    var dotColor: Color {
        switch self {
        case .attention: return Self.attentionColor
        case .working: return Self.workingColor
        case .quiet, .unknown: return Ember.ash
        case .closed: return Ember.ash.opacity(0.55)
        }
    }

    /// Card edge/accent. Quiet by default ("dark cockpit"): only the row that
    /// wants you lights up, so it pops by contrast rather than a wall of color.
    var accentColor: Color {
        switch self {
        case .attention: return Self.attentionColor
        case .working: return Self.workingColor.opacity(0.8)
        case .quiet, .unknown: return Ember.ash.opacity(0.4)
        case .closed: return Ember.ash.opacity(0.3)
        }
    }

    /// Status-label text color, demoted relative to the dot.
    var statusColor: Color {
        switch self {
        case .attention: return Self.attentionColor
        case .working: return Self.workingColor
        case .closed: return Ember.textMuted
        case .quiet, .unknown: return Ember.textSecondary
        }
    }

    /// Motion is reserved for genuine live work. "Waiting on you" is a stable
    /// state, so attention is steady, not pulsing - avoids alarm fatigue.
    var pulses: Bool { self == .working }

    /// The signal one activity state carries on its own, before the row-level
    /// facts (closed, suppressed, pending interaction, the Helm idle override)
    /// that `resolve` layers on top. The widget and Live Activity read the
    /// same mapping, so a state never looks different there than in the app.
    static func forActivityState(_ activityState: String) -> TimelineSignal {
        switch activityState {
        case "thinking", "executing":
            return .working
        case "blocked", "stalled":
            return .attention
        case "quiescent":
            return .quiet
        default:
            return .unknown
        }
    }

    /// Resolve the attention signal from a session's runtime facts. The optional
    /// `suppressed` flag lets a surface force `.quiet` (e.g. the app suppresses
    /// per-row attention while a global connectivity banner owns severity).
    /// Pending interaction and provider activity are independent facts.
    static func resolve(for session: SessionSummary, suppressed: Bool = false, asOf now: Date = Date()) -> TimelineSignal {
        if session.isClosed { return .closed }
        if suppressed { return .quiet }
        let facts = session.stateFacts
        if facts.workClaimExpired(asOf: now) { return .unknown }
        if session.needsAttention { return .attention }
        let keyedInteraction = facts.pendingInteractionKind != nil
            || facts.primary?.key == "needs_answer" || facts.primary?.key == "needs_approval"
        let tone = facts.primary?.tone
        if !keyedInteraction && (tone == "running" || tone == "thinking" || tone == "active"
            || facts.activityState == "thinking" || facts.activityState == "executing") {
            return .working
        }
        switch facts.activityState {
        case "blocked", "stalled": return .attention
        case "unknown": return facts.primary?.key == "idle" ? .quiet : .unknown
        default: return .quiet
        }
    }
}
