import SwiftUI

/// Pure, testable mapping from a session's runtime/capability state to the
/// redesign's "color is signal" vocabulary. Extracted out of `SessionRuntimeDock`
/// so the trust-state mapping is a unit-tested contract rather than buried view
/// code: the single state dot and the capability label are the ONLY color in the
/// chrome, and degraded states must stay loud.
///
/// Discipline rules this encodes:
///   • The dot is the only signal — running/thinking = live (flame),
///     blocked = attention (ember), idle = idle (cooled ash), everything
///     else = dormant (dimmer ash).
///   • Capability color is monochrome (secondary) UNLESS it's a warning, which
///     stays loud (ember). "success" is shown as a small live dot, not colored text.
///   • None of this gates sending — `canSendLive` is the hard gate elsewhere.
enum RuntimeSignal: Equatable {
    case live        // executing / healthy
    case attention   // blocked / needs the user
    case idle        // quiet, ready
    case dormant     // unknown / ended / offline

    var color: Color {
        switch self {
        case .live: return TranscriptPalette.live
        case .attention: return TranscriptPalette.attention
        case .idle: return Ember.ash
        case .dormant: return Ember.ash.opacity(0.55)
        }
    }
}

enum CapabilitySignal: Equatable {
    case live        // success — show a small live presence dot
    case warning     // degraded — stays loud (ember)
    case neutral     // monochrome secondary

    var color: Color {
        switch self {
        case .warning: return TranscriptPalette.attention
        default: return .secondary
        }
    }

    var showsLiveDot: Bool { self == .live }
}

struct RuntimeChromeStyle: Equatable {
    let dot: RuntimeSignal
    let capability: CapabilitySignal

    init(runtimeTone: String, capabilityTone: String) {
        // Maps the canonical session_state presentation tone vocabulary:
        // stalled / blocked / running / thinking / idle / active / inactive /
        // quiet / closed. `active` = process-observed alive (live);
        // `blocked`/`stalled` are degraded and must stay loud (attention);
        // `inactive` and `quiet` are idle.
        // Unknown tones fall to dormant.
        switch runtimeTone {
        case "running", "thinking", "active": dot = .live
        case "blocked", "stalled": dot = .attention
        case "idle", "inactive", "quiet": dot = .idle
        case "closed": dot = .dormant
        default: dot = .dormant
        }
        switch capabilityTone {
        case "success": capability = .live
        case "warning": capability = .warning
        default: capability = .neutral
        }
    }

    init(detail: SessionDetail) {
        self.init(runtimeTone: detail.runtimeTone, capabilityTone: detail.runtimeCapabilityTone)
    }
}
