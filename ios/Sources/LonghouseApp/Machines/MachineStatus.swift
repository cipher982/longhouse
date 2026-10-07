import SwiftUI

/// The machine signal role is intentionally independent of the older Ember
/// fire ramp. The role comes from the served tone, so every machine row
/// (directory, summary, and launch picker) shows the web's words and colour.
enum MachineStatusRole: Equatable {
    case live
    /// Connected with nothing running: the live dot, quieter words.
    case idle
    case attention
    case fault
    case quiet
    case off

    var dotColor: Color {
        switch self {
        case .live, .idle: return Ember.signalLive
        case .attention: return Ember.signalAttention
        case .fault: return Ember.signalFault
        case .quiet: return Ember.signalQuiet
        case .off: return Ember.signalOff
        }
    }

    var textColor: Color {
        switch self {
        case .live: return Ember.signalLiveText
        case .idle: return Ember.textSecondary
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

/// The machine's status words and colour, as the Runtime Host serves them
/// (server/zerg/services/machine_status.py): the summary's status when the
/// summary has loaded, otherwise the directory entry's own. The app adds only
/// a "last seen" age for an offline machine, which needs this device's clock.
/// Supplying `now` keeps this deterministic for unit tests.
func machineStatus(
    machine: MachineDirectoryEntry,
    summaryStatus: MachineServedStatus? = nil,
    now: Date = Date()
) -> MachineStatus {
    guard let served = summaryStatus ?? machine.status else {
        // A host that predates served machine status: say only what the
        // directory itself knows.
        return machine.online
            ? MachineStatus(text: "Online", role: .live)
            : MachineStatus(text: "Offline", detail: lastSeenText(machine.lastSeenAt, now: now), role: .off)
    }
    let role: MachineStatusRole
    switch served.tone {
    case "live": role = .live
    case "idle": role = .idle
    case "attention": role = .attention
    case "fault": role = .fault
    case "quiet": role = .quiet
    default: role = .off
    }
    let detail = served.hint ?? (role == .off ? lastSeenText(machine.lastSeenAt, now: now) : nil)
    return MachineStatus(text: served.label, detail: detail, role: role)
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
