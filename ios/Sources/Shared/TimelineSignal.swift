import SwiftUI

/// The single attention axis for a timeline row, shared by the app card and the
/// home-screen widget (and mirrored on web in lib/sessionRuntime.ts). Three
/// semantic stops the user can read pre-attentively, plus a closed/quiet rest:
///   - attention: the session is WAITING ON YOU — steady amber, never pulses.
///   - working:   the session is actively running — teal, breathing (live only).
///   - quiet:     idle/stale — grey, static.
///   - closed:    ended — dimmed grey, static.
/// Provider identity color stays on the glyph; it never bleeds into this axis.
enum TimelineSignal {
    case attention
    case working
    case quiet
    case closed

    /// Amber for "needs you". Separated from teal/grey on luminance + hue so it
    /// survives colorblindness; the status label text is the redundant code.
    static let amber = Color(red: 0.91, green: 0.64, blue: 0.24)
    static let teal = Color(red: 0.24, green: 0.71, blue: 0.78)

    /// The leading dot color — the loudest at-a-glance signal.
    var dotColor: Color {
        switch self {
        case .attention: return Self.amber
        case .working: return Self.teal
        case .quiet: return .secondary
        case .closed: return .secondary.opacity(0.6)
        }
    }

    /// Card edge/accent. Quiet by default ("dark cockpit"): only the row that
    /// wants you lights up, so it pops by contrast rather than a wall of color.
    var accentColor: Color {
        switch self {
        case .attention: return Self.amber
        case .working: return Self.teal.opacity(0.8)
        case .quiet: return .secondary.opacity(0.4)
        case .closed: return .secondary.opacity(0.3)
        }
    }

    /// Status-label text color, demoted relative to the dot.
    var statusColor: Color {
        switch self {
        case .attention: return Self.amber
        case .working: return Self.teal
        case .closed: return .secondary.opacity(0.7)
        case .quiet: return .secondary
        }
    }

    /// Motion is reserved for genuine live work. "Waiting on you" is a STABLE
    /// state, so attention is steady, not pulsing — avoids alarm fatigue.
    var pulses: Bool { self == .working }

    /// Spoken equivalent of the dot color, so the attention axis reaches
    /// VoiceOver instead of being color-only.
    var accessibilityState: String {
        switch self {
        case .attention: return "Waiting on you"
        case .working: return "Working"
        case .quiet: return "Idle"
        case .closed: return "Closed"
        }
    }

    /// Resolve the attention signal from a session's runtime facts. The optional
    /// `suppressed` flag lets a surface force `.quiet` (e.g. the app suppresses
    /// per-row attention while a global connectivity banner owns severity).
    /// `needs_attention` (curated) drives amber, NOT the raw needs_user state.
    static func resolve(for session: SessionSummary, suppressed: Bool = false) -> TimelineSignal {
        if session.isClosed { return .closed }
        if suppressed { return .quiet }
        if session.needsAttention { return .attention }

        let tone = session.timelineStatusTone.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let live = session.runtimeDisplay.activityRecency == "live"
        switch tone {
        case "thinking", "running":
            // Only animate genuinely live work; a stale "running" must not pulse.
            return live ? .working : .quiet
        case "blocked", "stalled":
            return .attention
        default:
            return .quiet
        }
    }
}
