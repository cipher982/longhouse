import SwiftUI

/// Pure, testable mapping from a session's runtime/capability state to the
/// redesign's "color is signal" vocabulary. Extracted out of `SessionRuntimeDock`
/// so the trust-state mapping is a unit-tested contract rather than buried view
/// code: the single state dot and the capability label are the ONLY color in the
/// chrome, and degraded states must stay loud.
///
/// Discipline rules this encodes:
///   • The dot is the only signal — running/thinking = live (green),
///     blocked = attention (orange), idle = idle (quiet grey), everything
///     else = dormant (grey).
///   • Capability color is monochrome (secondary) UNLESS it's a warning, which
///     stays loud (orange). "success" is shown as a small live dot, not green text.
///   • None of this gates sending — `canSendLive` is the hard gate elsewhere.
enum RuntimeSignal: Equatable {
    case live        // executing / healthy
    case attention   // blocked / needs the user
    case idle        // quiet, ready
    case dormant     // unknown / ended / offline

    var color: Color {
        switch self {
        case .live: return .green
        case .attention: return .orange
        case .idle: return Color(.systemGray2)
        case .dormant: return Color(.systemGray)
        }
    }
}

enum CapabilitySignal: Equatable {
    case live        // success — show a small green presence dot
    case warning     // degraded — stays loud (orange)
    case neutral     // monochrome secondary

    var color: Color {
        switch self {
        case .warning: return .orange
        default: return .secondary
        }
    }

    var showsLiveDot: Bool { self == .live }
}

struct RuntimeChromeStyle: Equatable {
    let dot: RuntimeSignal
    let capability: CapabilitySignal

    init(runtimeTone: String, capabilityTone: String) {
        switch runtimeTone {
        case "running", "thinking": dot = .live
        case "blocked": dot = .attention
        case "idle": dot = .idle
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
