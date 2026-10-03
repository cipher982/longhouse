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
    let syncFresh = sync.map { !$0.stale } ?? false
    let blockedBy = machine.launch.blockedBy

    // Repair is deliberately first. A live session does not hide a broken
    // shipping/control path that needs the person's attention.
    if blockedBy == "auth_failed"
        || blockedBy == "runtime_unreachable"
        || (syncFresh && sync?.status.lowercased() == "broken") {
        return MachineStatus(
            text: "Needs repair",
            detail: "Run longhouse local-health on this machine to inspect the fault",
            role: .fault
        )
    }

    if machine.online {
        let needs = machineSignInNeed(machine)
        if liveCount > 0 {
            return MachineStatus(text: "\(liveCount) live", detail: needs?.hint, role: .live)
        }
        if let needs {
            return MachineStatus(text: needs.label, detail: needs.hint, role: .attention)
        }
        switch blockedBy {
        case "engine_too_old":
            return MachineStatus(
                text: "Update required",
                detail: "Update Longhouse on this machine",
                role: .attention
            )
        case "no_launch_support":
            // Amber, as on the web: connected but unable to start a session.
            return MachineStatus(text: "Can't start sessions", role: .attention)
        default:
            break
        }
        // A directory-only machine may not have launch metadata yet. It is
        // connected, but an absent summary must never be rendered as idle.
        if activity == nil {
            if !machine.launch.providers.isEmpty {
                return MachineStatus(text: "Ready", role: .live)
            }
            return MachineStatus(text: "Online", role: .live)
        }
        return MachineStatus(text: "Online, idle", role: .live)
    }

    if liveCount > 0 {
        return MachineStatus(text: "\(liveCount) live", role: .live)
    }
    if syncFresh {
        return MachineStatus(text: "Sync only", role: .quiet)
    }
    return MachineStatus(
        text: "Offline",
        detail: lastSeenText(machine.lastSeenAt, now: now),
        role: .off
    )
}


private struct MachineSignInNeed {
    let label: String
    let hint: String
}

private func machineSignInNeed(_ machine: MachineDirectoryEntry) -> MachineSignInNeed? {
    let unavailable = machine.launch.unavailableProviders
    let signedOut = unavailable.filter { $0.reason == "not_authenticated" }
    // A missing CLI only matters when it leaves nothing launchable; otherwise
    // it is an agent this person simply does not use.
    let missing = machine.launch.providers.isEmpty
        ? unavailable.filter { $0.reason == "cli_missing" }
        : []
    let actionable = signedOut.isEmpty ? missing : signedOut
    guard !actionable.isEmpty else { return nil }

    let signedOutState = !signedOut.isEmpty
    let verb = signedOutState ? "signed out" : "not installed"
    if actionable.count == 1, let item = actionable.first {
        let name = ProviderBrands.displayName(item.provider)
        let fallback = signedOutState
            ? "Sign in to \(name) on \(machine.machineName)"
            : "Install \(name) on \(machine.machineName)"
        return MachineSignInNeed(
            label: "\(name) \(verb)",
            hint: item.remediation ?? fallback
        )
    }
    let names = actionable
        .map { ProviderBrands.displayName($0.provider) }
        .sorted()
        .joined(separator: " and ")
    return MachineSignInNeed(
        label: "\(actionable.count) agents \(verb)",
        hint: "\(signedOutState ? "Sign in to" : "Install") \(names) on \(machine.machineName)"
    )
}

func machineRelativeTime(_ raw: String?, now: Date = Date()) -> String? {
    guard let raw, let date = parseMachineDate(raw), date <= now else { return nil }
    let formatter = RelativeDateTimeFormatter()
    formatter.unitsStyle = .full
    return formatter.localizedString(for: date, relativeTo: now)
}

private func lastSeenText(_ raw: String?, now: Date) -> String? {
    guard let relative = machineRelativeTime(raw, now: now) else { return nil }
    return "last seen \(relative)"
}

private func parseMachineDate(_ raw: String) -> Date? {
    LonghouseDateParser.parse(raw)
}
