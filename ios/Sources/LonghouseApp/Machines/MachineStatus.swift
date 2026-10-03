import SwiftUI

/// The machine signal role is intentionally independent of the older Ember
/// fire ramp. Every machine row (directory, summary, and launch picker) uses
/// this one derivation so status words and colour stay in lockstep.
enum MachineStatusRole: Equatable {
    case live
    case attention
    case fault
    case quiet
    case off

    var dotColor: Color {
        switch self {
        case .live: return Ember.signalLive
        case .attention: return Ember.signalAttention
        case .fault: return Ember.signalFault
        case .quiet: return Ember.signalQuiet
        case .off: return Ember.signalOff
        }
    }

    var textColor: Color {
        switch self {
        case .live: return Ember.signalLiveText
        case .attention: return Ember.signalAttentionText
        case .fault: return Ember.signalFaultText
        case .quiet: return Ember.signalQuietText
        case .off: return Ember.signalQuietText
        }
    }
}

struct MachineStatus: Equatable {
    let text: String
    let detail: String?
    let role: MachineStatusRole

    init(text: String, detail: String? = nil, role: MachineStatusRole) {
        self.text = text
        self.detail = detail
        self.role = role
    }
}

/// Derive the product status words from the directory plus optional summary.
/// Supplying `now` keeps this a pure, deterministic function for unit tests;
/// production callers use its default value.
func deriveMachineStatus(
    machine: MachineDirectoryEntry,
    activity: MachineActivity? = nil,
    sync: MachineSync? = nil,
    now: Date = Date()
) -> MachineStatus {
    let liveCount = activity?.liveCount ?? 0
    let unavailable = machine.launch.unavailableProviders

    // Live takes precedence over attention in the primary status. The detail
    // remains available for the row/detail sign-in hint.
    if machine.online, liveCount > 0 {
        return MachineStatus(
            text: "\(liveCount) live",
            detail: unavailableHint(unavailable),
            role: .live
        )
    }

    if machine.online, let attention = unavailableStatus(unavailable) {
        return MachineStatus(
            text: attention,
            detail: unavailableHint(unavailable),
            role: .attention
        )
    }

    if sync?.status.lowercased() == "broken" {
        return MachineStatus(text: "Needs repair", role: .fault)
    }
    switch machine.launch.blockedBy {
    case "auth_failed", "runtime_unreachable":
        return MachineStatus(text: "Needs repair", role: .fault)
    case "engine_too_old":
        return MachineStatus(text: "Update required", role: .attention)
    case "no_launch_support":
        return MachineStatus(text: "Can't start sessions", role: .quiet)
    default:
        break
    }

    if !machine.online,
       machine.launch.blockedBy == "control_down",
       let sync,
       !sync.stale {
        return MachineStatus(text: "Sync only", role: .quiet)
    }

    if machine.online {
        // The directory-only launch chooser has no activity snapshot. A
        // launchable online machine is ready, not "idle" (which would claim
        // knowledge of its live session set).
        if activity == nil, !machine.launch.providers.isEmpty {
            return MachineStatus(text: "Ready", role: .live)
        }
        return MachineStatus(text: "Online, idle", role: .live)
    }

    return MachineStatus(
        text: "Offline",
        detail: lastConnectedText(machine.lastSeenAt, now: now),
        role: .off
    )
}

private func unavailableStatus(_ unavailable: [MachineLaunchUnavailableProvider]) -> String? {
    guard let item = unavailable.sorted(by: { $0.provider < $1.provider }).first else { return nil }
    let name = ProviderBrands.displayName(item.provider)
    if item.reason == "cli_missing" {
        return "\(name) not installed"
    }
    return "\(name) signed out"
}
private func unavailableHint(_ unavailable: [MachineLaunchUnavailableProvider]) -> String? {
    guard !unavailable.isEmpty else { return nil }
    if unavailable.count == 1, let remediation = unavailable[0].remediation, !remediation.isEmpty {
        return remediation
    }
    let names = unavailable
        .map { ProviderBrands.displayName($0.provider) }
        .sorted()
        .joined(separator: " or ")
    if unavailable.allSatisfy({ $0.reason == "cli_missing" }) {
        return "Install \(names) on this machine"
    }
    return "Sign in to \(names) on this machine"
}

func machineRelativeTime(_ raw: String?, now: Date = Date()) -> String? {
    guard let raw, let date = parseMachineDate(raw), date <= now else { return nil }
    let formatter = RelativeDateTimeFormatter()
    formatter.unitsStyle = .full
    return formatter.localizedString(for: date, relativeTo: now)
}

private func lastConnectedText(_ raw: String?, now: Date) -> String? {
    guard let relative = machineRelativeTime(raw, now: now) else { return nil }
    return "last connected \(relative)"
}

private func parseMachineDate(_ raw: String) -> Date? {
    LonghouseDateParser.parse(raw)
}
