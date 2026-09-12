import SwiftUI
private let menuBarUnavailableReasons: Set<String> = [
    "engine_status_missing", "engine_status_unreadable", "engine_status_stale",
    "engine_projection_stale", "engine_reconciliation_failed",
    "engine_offline", "transport_unavailable",
]
private let menuBarTransportAttentionReasons: Set<String> = [
    "ship_stalled", "connect_errors", "server_errors", "rate_limited",
    "retryable_client_errors",
]


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
    public func menuBarPresentation(
        relativeTo referenceDate: Date,
        localEvidenceTrust: DataTrust = .current,
        projectionTrust: DataTrust = .current
    ) -> MenuBarPresentation {
        let sessions = currentManagedSessions
        let openHelmCount = foregroundManagedCount
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
            "storage_v2_outbox_unreadable",
            "storage_v2_sources_unresolved",
            "engine_status_unreadable", "orphaned_managed_bridge",
            "managed_launch_recovery_unreadable",
            "service_stopped", "service_not_installed", "service_generation_mismatch",
            "service_artifact_mismatch",
            "service_machine_name_mismatch", "service_state_hash_mismatch",
            "service_runner_name_mismatch", "desktop_app_setup_required",
            "desktop_app_wrong_install_location",
        ]
        let inspectReasons: Set<String> = Set([
            "archive_dead_lettered", "archive_repair_paused", "orphaned_managed_bridge",
            "managed_session_control_degraded", "provider_release_blocked",
            "storage_v2_sources_proof_unknown", "managed_launch_recovery_active",
            "managed_launch_recovery_exhausted", "parse_errors",
            "payload_rejected", "payload_too_large", "spool_dead", "spool_dead_letters",
        ]).union(menuBarTransportAttentionReasons)
        let transportAttentionReason = reasons.first {
            menuBarTransportAttentionReasons.contains($0)
        }
        let localEvidenceUnavailable = !localEvidenceTrust.isCurrent
        let projectionUnavailable = !projectionTrust.isCurrent
        // This producer red state is deliberately row-level: the engine has
        // preserved the session, but the phase contract is newer than this
        // client. Keep it visible in the session row without turning an
        // otherwise healthy local machine into a repair alarm. Every other
        // native red state remains machine-wide repair unless a concrete
        // repair reason already says so.
        let rowLevelRedReasons: Set<String> = ["managed_unknown_phase"]
        let storageBlockRequiresRepair = self.storageBlockRequiresRepair
        let storageBlockIsRecovering = self.storageBlockIsRecovering
        let deadLetterCount = max(
            engineStatus?.payload?.spoolDeadCount ?? 0,
            reasons.contains("spool_dead") || reasons.contains("spool_dead_letters") ? 1 : 0
        )
        let hasDeadLetters = deadLetterCount > 0
        let nativeRedRequiresRepair = parsedSeverity == .red
            && rowLevelRedReasons.isDisjoint(with: reasons)

        let promotion: MenuBarPromotion
        if nativeRedRequiresRepair
            || storageBlockRequiresRepair
            || !repairReasons.isDisjoint(with: reasons)
            || isSetupRequired
            || isInstallLocationBlocked {
            promotion = .repair
        } else if needsUser > 0 {
            promotion = .needsUser
        } else if localEvidenceUnavailable || projectionUnavailable {
            promotion = .unavailable
        } else if storageBlockIsRecovering || storageBlockProofUnknown || degraded > 0 || orphanBridgeCount > 0 || transportAttentionReason != nil || !inspectReasons.isDisjoint(with: reasons) {
            promotion = .inspect
        } else if !menuBarUnavailableReasons.isDisjoint(with: reasons)
            || engineStatus?.error != nil
            || engineStatus?.fresh == false {
            promotion = .unavailable
        } else {
            promotion = .normal
        }

        let headline: String
        switch promotion {
        case .repair where storageBlockRequiresRepair:
            let count = storageUnresolvedBlockCount > 0 ? storageUnresolvedBlockCount : storageBlockedCount
            headline = "Durable upload needs inspection for \(count) source\(count == 1 ? "" : "s")"
        case .repair where isSetupRequired:
            headline = "Finish setup on this Mac"
        case .repair where isInstallLocationBlocked:
            headline = "Move Longhouse to Applications"
        case .repair:
            headline = "Local shipping needs repair"
        case .needsUser:
            headline = "\(needsUser) session\(needsUser == 1 ? "" : "s") need\(needsUser == 1 ? "s" : "") you"
        case .inspect where storageBlockIsRecovering:
            headline = "Source upload reconciliation pending for \(storageBlockedCount) source\(storageBlockedCount == 1 ? "" : "s")"
        case .inspect where storageBlockProofUnknown:
            headline = "Durable upload proof unavailable for \(storageBlockedCount) source\(storageBlockedCount == 1 ? "" : "s")"
        case .inspect where hasDeadLetters:
            headline = "Durable upload needs inspection for \(deadLetterCount) dead letter\(deadLetterCount == 1 ? "" : "s")"
        case .inspect where reasons.contains("managed_launch_recovery_exhausted"):
            headline = "Managed session recovery needs attention"
        case .inspect where transportAttentionReason != nil:
            headline = "Local upload needs attention"
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
            facts: menuBarSystemFacts(
                relativeTo: referenceDate,
                localEvidenceTrust: localEvidenceTrust,
                projectionTrust: projectionTrust
            ),
            backgroundActivity: archiveBackgroundActivity
        )
    }

    private func menuBarSystemFacts(
        relativeTo referenceDate: Date,
        localEvidenceTrust: DataTrust,
        projectionTrust: DataTrust
    ) -> [MenuBarSystemFact] {
        let localEvidenceUnavailable = !localEvidenceTrust.isCurrent
        let projectionUnavailable = !projectionTrust.isCurrent
        let deadLetterCount = max(
            engineStatus?.payload?.spoolDeadCount ?? 0,
            reasons.contains("spool_dead") || reasons.contains("spool_dead_letters") ? 1 : 0
        )
        let hasDeadLetters = deadLetterCount > 0
        // Native health intentionally has no service-manager block. A
        // fresh engine pulse with a daemon pid is sufficient local-process
        // evidence; otherwise the panel reports Unknown instead of inventing
        // a service failure.
        let localAgentRunning: Bool
        if localEvidenceUnavailable {
            localAgentRunning = false
        } else if service != nil {
            localAgentRunning = serviceStatusLabel == "running"
        } else {
            localAgentRunning = engineStatus?.fresh == true && engineStatus?.payload?.daemonPid != nil
        }
        let localValue: String
        if localEvidenceUnavailable || engineStatus?.fresh == false {
            localValue = "Unknown"
        } else if service != nil {
            localValue = localAgentRunning ? "Running" : serviceStatusTitle
        } else {
            localValue = localAgentRunning ? "Running" : "Unknown"
        }
        let freshnessValue = engineFreshnessValueLabel(relativeTo: referenceDate)
        let freshnessIsCurrent = !localEvidenceUnavailable && freshnessValue.hasPrefix("Fresh")
        let localPromotion: MenuBarPromotion = localEvidenceUnavailable || engineStatus?.fresh == false
            ? .unavailable
            : service != nil && !localAgentRunning
            ? .repair
            : localAgentRunning && freshnessIsCurrent ? .normal : .unavailable

        let controlLimited = hasLimitedCanonicalControl
        let controlValue = projectionUnavailable
            ? "Unavailable"
            : controlLimited ? "Limited" : hasCanonicalControlTruth ? "Connected" : "Unavailable"
        let controlPromotion: MenuBarPromotion = projectionUnavailable
            ? .unavailable
            : controlLimited
            ? .inspect
            : hasCanonicalControlTruth ? .normal : .unavailable

        // Without an engine payload there is no upload or transport evidence at
        // all. Reporting "Clear" and "Connected" from missing evidence is a
        // false negative: it claims a healthy state nobody observed.
        let hasEngineEvidence = engineStatus?.payload != nil

        let durableValue: String
        let durablePromotion: MenuBarPromotion
        let localEngineEvidenceUnavailable = localEvidenceUnavailable
            || engineStatus?.fresh == false
            || reasons.contains("engine_projection_stale")
            || reasons.contains("engine_reconciliation_failed")
        if !hasEngineEvidence || localEngineEvidenceUnavailable {
            durableValue = "Unknown"
            durablePromotion = reasons.contains("engine_reconciliation_failed") ? .repair : .unavailable
        } else if reasons.contains("storage_v2_outbox_unreadable")
                    || engineStatus?.payload?.storageV2Outbox?.malformedCounter == true
                    || storageBlockProofUnknown {
            durableValue = "Unknown"
            durablePromotion = reasons.contains("storage_v2_outbox_unreadable") ? .repair : .inspect
        } else if storageBlockedCount > 0 {
            durableValue = "\(storageBlockedCount) source conflict\(storageBlockedCount == 1 ? "" : "s")"
            durablePromotion = storageBlockRequiresRepair ? .repair : .inspect
        } else if hasDeadLetters {
            durableValue = "\(deadLetterCount) dead letter\(deadLetterCount == 1 ? "" : "s")"
            durablePromotion = .inspect
        } else if engineStatus?.payload?.shippingProgress?.pendingWork == true {
            let stalled = engineStatus?.payload?.shippingProgress?.stalled == true
                || reasons.contains("ship_stalled")
            durableValue = stalled ? "Stalled" : "Pending"
            durablePromotion = stalled ? .inspect : .normal
        } else if storagePendingCount > 0 {
            durableValue = "\(storagePendingCount) pending"
            durablePromotion = .normal
        } else {
            durableValue = "Clear"
            durablePromotion = .normal
        }

        let transportAttentionReason = reasons.first {
            menuBarTransportAttentionReasons.contains($0)
        }
        let nativeTransport = transport
        let transportValue: String
        let transportDetail: String?
        let transportPromotion: MenuBarPromotion
        if !hasEngineEvidence || localEvidenceUnavailable || projectionUnavailable {
            transportValue = "Unknown"
            transportDetail = projectionUnavailable
                ? "Runtime Host projection is unavailable"
                : localEvidenceUnavailable ? "local status evidence is unavailable" : "no engine evidence"
            transportPromotion = .unavailable
        } else if engineStatus?.fresh == false {
            transportValue = "Unknown"
            transportDetail = "engine evidence is stale"
            transportPromotion = .unavailable
        } else if nativeTransport?.status?.lowercased() == "unknown" {
            transportValue = "Unknown"
            transportDetail = nativeTransport?.statusSummary ?? nativeTransport?.statusReason
            transportPromotion = .unavailable
        } else if engineStatus?.payload?.isOffline == true {
            transportValue = "Offline"
            transportDetail = "data retained locally"
            transportPromotion = .unavailable
        } else if let unavailableReason = reasons.first(where: { menuBarUnavailableReasons.contains($0) }) {
            transportValue = "Unknown"
            transportDetail = unavailableReason.replacingOccurrences(of: "_", with: " ")
            transportPromotion = .unavailable
        } else if let transportAttentionReason {
            transportValue = "Retrying"
            transportDetail = nativeTransport?.statusSummary
                ?? transportAttentionReason.replacingOccurrences(of: "_", with: " ")
            transportPromotion = .inspect
        } else if nativeTransport?.status?.lowercased() == "degraded" {
            transportValue = "Retrying"
            transportDetail = nativeTransport?.statusSummary ?? nativeTransport?.statusReason
            transportPromotion = .inspect
        } else if nativeTransport?.status?.lowercased() == "broken" {
            transportValue = "Unknown"
            transportDetail = nativeTransport?.statusSummary ?? nativeTransport?.statusReason
            transportPromotion = .inspect
        } else {
            transportValue = "Connected"
            transportDetail = nil
            transportPromotion = .normal
        }

        let durableDetail: String
        if hasDeadLetters {
            durableDetail = "\(deadLetterCount) dead letter\(deadLetterCount == 1 ? "" : "s") retained · inspect shipping before retrying"
        } else if durableValue == "Stalled",
           let seconds = engineStatus?.payload?.shippingProgress?.secondsWithoutProgress,
           seconds > 0 {
            durableDetail = "no progress \(Self.compactSeconds(seconds)) · last receipt \(lastShipValueLabel(relativeTo: referenceDate))"
        } else if durableValue == "Unknown" {
            durableDetail = "last known receipt \(lastShipValueLabel(relativeTo: referenceDate))"
        } else {
            durableDetail = "last receipt \(lastShipValueLabel(relativeTo: referenceDate))"
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
                detail: durableDetail,
                promotion: durablePromotion
            ),
            MenuBarSystemFact(
                id: "transport", label: "Transport",
                value: transportValue, detail: transportDetail,
                promotion: transportPromotion
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

    private static func compactSeconds(_ value: UInt64) -> String {
        let seconds = min(value, UInt64(Int.max))
        if seconds < 60 {
            return "\(seconds)s"
        }
        if seconds < 3600 {
            return "\(seconds / 60)m"
        }
        return "\(seconds / 3600)h"
    }
}

extension ManagedSessionSnapshot {
    var explicitlyNeedsUser: Bool {
        if menuBarAttentionKind == .needsYou { return true }
        let normalized = phase?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
        return normalized == "needs permission" || normalized == "needs user"
    }
}
