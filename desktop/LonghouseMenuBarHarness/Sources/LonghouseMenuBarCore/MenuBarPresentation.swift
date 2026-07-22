import SwiftUI

public enum MenuBarPromotion: String, Equatable, Sendable {
    case normal
    case needsUser
    case inspect
    case unavailable
    case repair

    public var accentColor: Color {
        switch self {
        case .normal:
            return Color(red: 0.17, green: 0.70, blue: 0.39)
        case .needsUser:
            return .blue
        case .inspect:
            return Color(red: 0.90, green: 0.67, blue: 0.16)
        case .unavailable:
            return Color(red: 0.48, green: 0.53, blue: 0.58)
        case .repair:
            return Color(red: 0.86, green: 0.29, blue: 0.23)
        }
    }

    public var statusLabel: String {
        switch self {
        case .normal: return "Current"
        case .needsUser: return "Needs you"
        case .inspect: return "Inspect"
        case .unavailable: return "Unknown"
        case .repair: return "Repair"
        }
    }

    public var iconSeverity: HarnessSeverity {
        switch self {
        case .normal, .needsUser: return .green
        case .inspect: return .yellow
        case .unavailable: return .gray
        case .repair: return .red
        }
    }
}

public struct MenuBarSystemFact: Identifiable, Equatable, Sendable {
    public let id: String
    public let label: String
    public let value: String
    public let detail: String?
    public let promotion: MenuBarPromotion
}

public struct MenuBarPresentation: Equatable, Sendable {
    public let promotion: MenuBarPromotion
    public let headline: String
    public let subheadline: String
    public let facts: [MenuBarSystemFact]
    public let backgroundActivity: String?

    public var needsStatusItemBadge: Bool { promotion != .normal }
}

extension HealthSnapshot {
    public func menuBarPresentation(relativeTo referenceDate: Date) -> MenuBarPresentation {
        let sessions = currentManagedSessions
        let openHelmCount = foregroundManagedCount + legacyAttachedManagedCount
        let needsUser = sessions.filter { $0.explicitlyNeedsUser }.count
        let working = sessions.filter { $0.menuBarAttentionKind == .working }.count
        let blocked = sessions.filter { $0.menuBarAttentionKind == .blocked && !$0.explicitlyNeedsUser }.count
        let unavailable = sessions.filter { $0.menuBarAttentionKind == .phaseUnavailable }.count
        let unknown = sessions.filter {
            if case .unknown = $0.menuBarAttentionKind { return true }
            return false
        }.count
        let degraded = sessions.filter {
            $0.menuBarAttentionKind == .degraded || $0.menuBarAttentionKind == .detached
        }.count
        let idle = max(0, sessions.count - needsUser - working - blocked - degraded - unavailable - unknown)

        let repairReasons: Set<String> = [
            "storage_v2_sources_blocked", "storage_v2_outbox_unreadable",
            "service_stopped", "spool_dead", "desktop_app_setup_required",
            "desktop_app_wrong_install_location",
        ]
        let unavailableReasons: Set<String> = [
            "engine_status_missing", "engine_status_unreadable", "engine_status_stale",
        ]
        let inspectReasons: Set<String> = [
            "archive_dead_lettered", "orphaned_managed_bridge",
            "managed_session_control_degraded", "provider_release_blocked",
        ]

        let promotion: MenuBarPromotion
        if !repairReasons.isDisjoint(with: reasons) || isSetupRequired || isInstallLocationBlocked {
            promotion = .repair
        } else if needsUser > 0 {
            promotion = .needsUser
        } else if degraded > 0 || orphanBridgeCount > 0 || !inspectReasons.isDisjoint(with: reasons) {
            promotion = .inspect
        } else if !unavailableReasons.isDisjoint(with: reasons) || engineStatus?.error != nil {
            promotion = .unavailable
        } else {
            promotion = .normal
        }

        let headline: String
        switch promotion {
        case .repair where storageBlockedCount > 0:
            headline = "Durable upload blocked for \(storageBlockedCount) source\(storageBlockedCount == 1 ? "" : "s")"
        case .repair where isSetupRequired:
            headline = "Finish setup on this Mac"
        case .repair where isInstallLocationBlocked:
            headline = "Move Longhouse to Applications"
        case .repair:
            headline = "Local shipping needs repair"
        case .needsUser:
            headline = "\(needsUser) session\(needsUser == 1 ? "" : "s") need\(needsUser == 1 ? "s" : "") you"
        case .inspect where degraded > 0:
            headline = "Remote control unavailable for \(degraded) session\(degraded == 1 ? "" : "s")"
        case .inspect where orphanBridgeCount > 0:
            headline = "\(orphanBridgeCount) background process\(orphanBridgeCount == 1 ? "" : "es") need cleanup"
        case .inspect:
            headline = "Historical archive needs review"
        case .unavailable:
            headline = "Current local status unavailable"
        case .normal where openHelmCount > 0:
            headline = "\(openHelmCount) Helm session\(openHelmCount == 1 ? "" : "s") open"
        case .normal where backgroundManagedCount > 0:
            headline = "\(backgroundManagedCount) background session\(backgroundManagedCount == 1 ? "" : "s")"
        case .normal:
            headline = "No sessions running"
        }

        var counts: [String] = []
        if working > 0 { counts.append("\(working) working") }
        if needsUser > 0 { counts.append("\(needsUser) waiting") }
        if idle > 0 { counts.append("\(idle) idle") }
        if blocked > 0 { counts.append("\(blocked) blocked") }
        if degraded > 0 { counts.append("\(degraded) limited") }
        if unavailable > 0 {
            counts.append("\(unavailable) phase\(unavailable == 1 ? "" : "s") unavailable")
        }
        if unknown > 0 { counts.append("\(unknown) unknown") }
        if backgroundManagedCount > 0 { counts.append("\(backgroundManagedCount) background") }
        counts.append("updated \(snapshotAgeCompactLabel(relativeTo: referenceDate))")

        return MenuBarPresentation(
            promotion: promotion,
            headline: headline,
            subheadline: counts.joined(separator: " · "),
            facts: menuBarSystemFacts(relativeTo: referenceDate),
            backgroundActivity: archiveBackgroundActivity
        )
    }

    private func menuBarSystemFacts(relativeTo referenceDate: Date) -> [MenuBarSystemFact] {
        let localValue = serviceStatusLabel == "running" ? "Running" : serviceStatusTitle
        let freshnessValue = engineFreshnessValueLabel(relativeTo: referenceDate)
        let freshnessIsCurrent = freshnessValue.hasPrefix("Fresh")
        let localPromotion: MenuBarPromotion = serviceStatusLabel != "running"
            ? .repair
            : freshnessIsCurrent ? .normal : .unavailable

        let controlLimited = hasLimitedCanonicalControl
        let controlValue = controlLimited ? "Limited" : hasCanonicalControlTruth ? "Connected" : "Unavailable"
        let controlPromotion: MenuBarPromotion = controlLimited
            ? .inspect
            : hasCanonicalControlTruth ? .normal : .unavailable

        let durableValue: String
        let durablePromotion: MenuBarPromotion
        if storageBlockedCount > 0 {
            durableValue = "\(storageBlockedCount) source conflict\(storageBlockedCount == 1 ? "" : "s")"
            durablePromotion = .repair
        } else if storagePendingCount > 0 {
            durableValue = "\(storagePendingCount) pending"
            durablePromotion = .normal
        } else {
            durableValue = "Clear"
            durablePromotion = .normal
        }

        return [
            MenuBarSystemFact(
                id: "local-agent", label: "Local agent", value: localValue,
                detail: "observed \(engineAgeLabel(relativeTo: referenceDate)) ago",
                promotion: localPromotion
            ),
            MenuBarSystemFact(
                id: "remote-control", label: "Remote control", value: controlValue,
                detail: hostValueLabel == "-" ? nil : "Runtime Host · \(hostValueLabel)",
                promotion: controlPromotion
            ),
            MenuBarSystemFact(
                id: "durable-upload", label: "Durable upload", value: durableValue,
                detail: "last receipt \(lastShipValueLabel(relativeTo: referenceDate))",
                promotion: durablePromotion
            ),
            MenuBarSystemFact(
                id: "transport", label: "Transport",
                value: engineStatus?.payload?.isOffline == true ? "Offline" : "Connected",
                detail: engineStatus?.payload?.isOffline == true ? "data retained locally" : nil,
                promotion: engineStatus?.payload?.isOffline == true ? .unavailable : .normal
            ),
            MenuBarSystemFact(
                id: "freshness", label: "Status freshness",
                value: freshnessValue, detail: "Local engine",
                promotion: freshnessIsCurrent ? .normal : .unavailable
            ),
        ]
    }

    public var archiveBackgroundActivity: String? {
        guard let archive = engineStatus?.payload?.archiveBacklog else { return nil }
        let state = (archive.state ?? "idle").lowercased()
        let pending = archive.pendingRanges ?? 0
        let pendingBytes = archive.pendingBytes ?? 0
        guard (state != "complete" && state != "idle") || pending > 0 else { return nil }
        let action = state == "uploading" ? "uploading" : state == "scanning" ? "scanning" : state
        return "Archive projection \(action) \(Self.compactBytes(pendingBytes)) · \(pending) range\(pending == 1 ? "" : "s")"
    }

    private static func compactBytes(_ value: Int) -> String {
        let units = ["B", "KB", "MB", "GB", "TB"]
        var scaled = Double(max(0, value))
        var index = 0
        while scaled >= 1024, index < units.count - 1 {
            scaled /= 1024
            index += 1
        }
        return index == 0 ? "\(Int(scaled)) \(units[index])" : String(format: "%.1f %@", scaled, units[index])
    }
}

extension ManagedSessionSnapshot {
    var explicitlyNeedsUser: Bool {
        if menuBarAttentionKind == .needsYou { return true }
        let normalized = phase?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
        return normalized == "needs permission" || normalized == "needs user"
    }
}
