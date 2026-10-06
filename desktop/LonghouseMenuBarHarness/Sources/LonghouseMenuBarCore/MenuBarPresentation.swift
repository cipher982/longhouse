import SwiftUI

private enum HostLinkCopy {
    static let updatingHeadline = "Longhouse is updating"
    static let updatingDetail = "Your agents keep running on this Mac. Nothing is lost; updates resume in a few seconds."
    static let slowUpdateHeadline = "Update is taking longer than usual"
}

private struct HostLinkDisplay {
    let headline: String
    let detail: String
    let promotion: MenuBarPromotion
}

private let menuBarUnavailableReasons: Set<String> = [
    "engine_status_missing", "engine_status_unreadable", "engine_status_stale",
    "engine_projection_stale",
    "engine_offline", "transport_unavailable",
]
private let menuBarTransportAttentionReasons: Set<String> = [
    "ship_stalled", "connect_errors", "server_errors", "rate_limited",
    "retryable_client_errors",
]
private let menuBarSessionDiscoveryReasons: Set<String> = [
    "engine_reconciling", "engine_reconciliation_stale", "engine_reconciliation_failed",
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
    /// The machine's own promotion and headline, ignoring sessions that need
    /// the user. The health line reads these so a waiting session cannot hide
    /// a system warning.
    public let systemPromotion: MenuBarPromotion
    public let systemHeadline: String
    public let detail: String?
    public let hostUpdateClaimIsValid: Bool
    public let facts: [MenuBarSystemFact]
    public let backgroundActivity: String?

    public var needsStatusItemBadge: Bool { promotion != .normal }
}

extension HealthSnapshot {
    // A host update is stronger evidence than a failed heartbeat from before its claim.
    private func heartbeatPostFailed(hasValidUpdateClaim: Bool) -> Bool {
        guard !hasValidUpdateClaim else { return false }
        return heartbeatTransport?.state == "degraded" || reasons.contains("heartbeat_post_failed")
    }

    private var heartbeatEvidenceRejected: Bool {
        reasons.contains("heartbeat_evidence_rejected")
            || ["disabled", "failed", "oversize_evidence", "rejected", "unsupported_schema"]
                .contains(heartbeatTransport?.evidenceState ?? "")
    }

    private func hostLinkDisplay(
        relativeTo referenceDate: Date,
        hasValidUpdateClaim: Bool
    ) -> HostLinkDisplay? {
        guard hasValidUpdateClaim, let hostLink else { return nil }
        switch hostLink.state {
        case "updating":
            guard let elapsed = hostLinkElapsedSeconds(relativeTo: referenceDate), elapsed >= 2 else {
                return nil
            }
            return HostLinkDisplay(
                headline: HostLinkCopy.updatingHeadline,
                detail: HostLinkCopy.updatingDetail,
                promotion: .unavailable
            )
        case "slow_update":
            let elapsed = hostLinkElapsedSeconds(relativeTo: referenceDate)
                .map { Self.compactSeconds(UInt64($0)) }
            let headline = elapsed.map { "\(HostLinkCopy.slowUpdateHeadline) · \($0)" }
                ?? HostLinkCopy.slowUpdateHeadline
            return HostLinkDisplay(
                headline: headline,
                detail: HostLinkCopy.updatingDetail,
                promotion: .inspect
            )
        default:
            return nil
        }
    }

    private func hostLinkElapsedSeconds(relativeTo referenceDate: Date) -> Int? {
        guard let raw = hostLink?.claimStartedAt,
              let startedAt = Self.parseISO8601(raw) else {
            return nil
        }
        let elapsed = referenceDate.timeIntervalSince(startedAt)
        guard elapsed.isFinite, elapsed >= 0 else { return nil }
        return Int(elapsed)
    }

    public func menuBarPresentation(
        relativeTo referenceDate: Date,
        localEvidenceTrust: DataTrust = .current,
        projectionTrust: DataTrust = .current
    ) -> MenuBarPresentation {
        let sessions = currentManagedSessions
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
            "orphaned_managed_bridge",
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
        let sessionDiscoveryAttention = self.sessionDiscoveryAttention
        let localEvidenceUnavailable = !localEvidenceTrust.isCurrent
        let projectionUnavailable = !projectionTrust.isCurrent
        let localStatusStale = engineStatus?.fresh == false
            || reasons.contains("engine_status_stale")
            || reasons.contains("engine_projection_stale")
        let hasValidUpdateClaim = hostLink?.hasValidUpdateClaim(relativeTo: referenceDate) == true
        let heartbeatPostFailed = self.heartbeatPostFailed(hasValidUpdateClaim: hasValidUpdateClaim)
        let hostUpdate: HostLinkDisplay?
        if localEvidenceUnavailable || engineStatus?.fresh == false {
            hostUpdate = nil
        } else {
            hostUpdate = hostLinkDisplay(
                relativeTo: referenceDate,
                hasValidUpdateClaim: hasValidUpdateClaim
            )
        }
        // preserved the session, but the phase contract is newer than this
        // client. Keep it visible in the session row without turning an
        // otherwise healthy local machine into a repair alarm. Discovery
        // reconciliation is another independent evidence lane: a failed or
        // in-progress scan must not erase current upload or control facts.
        let rowLevelRedReasons = menuBarSessionDiscoveryReasons.union(["managed_unknown_phase"])
        let storageBlockRequiresRepair = self.storageBlockRequiresRepair
        let storageBlockIsRecovering = self.storageBlockIsRecovering
        let deadLetterCount = max(
            engineStatus?.payload?.spoolDeadCount ?? 0,
            reasons.contains("spool_dead") || reasons.contains("spool_dead_letters") ? 1 : 0
        )
        let hasDeadLetters = deadLetterCount > 0
        // Ignore a red severity inherited from update-only reasons while a claim is valid.
        let hostUpdateIsOnlyReason = hasValidUpdateClaim
            && !reasons.isEmpty
            && reasons.allSatisfy {
                $0 == "heartbeat_post_failed" || $0 == "host_updating" || $0 == "host_update_slow"
            }
        let nativeRedRequiresRepair = parsedSeverity == .red
            && !hostUpdateIsOnlyReason
            && rowLevelRedReasons.isDisjoint(with: reasons)
            && !reasons.contains("engine_status_stale")
            && !reasons.contains("engine_projection_stale")
        // The machine's own state, before session attention is considered.
        // A session waiting on the user outranks inspect/unknown for the
        // header and badge, but must not erase them from the health line.
        var hostUpdateIsSystemState = false
        let systemPromotion: MenuBarPromotion
        if nativeRedRequiresRepair
            || storageBlockRequiresRepair
            || !repairReasons.isDisjoint(with: reasons)
            || isSetupRequired
            || isInstallLocationBlocked {
            systemPromotion = .repair
        } else if localEvidenceUnavailable {
            // The producer itself is not current. Do not reuse the
            // Runtime Host projection warning for this case: all local
            // facts are now explicitly last-known.
            systemPromotion = .unavailable
        } else if !menuBarUnavailableReasons.isDisjoint(with: reasons)
            || engineStatus?.error != nil
            || engineStatus?.fresh == false {
            systemPromotion = .unavailable
        } else if let hostUpdate {
            systemPromotion = hostUpdate.promotion
            hostUpdateIsSystemState = true
        } else if projectionUnavailable {
            // Runtime Host session projection is a separate evidence lane.
            // Losing it must not make a healthy local Machine Agent look
            // unavailable or offer a local-agent repair.
            systemPromotion = .inspect
        } else if storageBlockIsRecovering
                    || storageBlockProofUnknown
                    || degraded > 0
                    || orphanBridgeCount > 0
                    || transportAttentionReason != nil
                    || sessionDiscoveryAttention
                    || heartbeatPostFailed
                    || heartbeatEvidenceRejected
                    || !inspectReasons.isDisjoint(with: reasons) {
            systemPromotion = .inspect
        } else {
            systemPromotion = .normal
        }
        let promotion: MenuBarPromotion = systemPromotion == .repair
            ? .repair
            : needsUser > 0 ? .needsUser : systemPromotion

        func headlineText(for promotion: MenuBarPromotion) -> String {
            switch promotion {
            case .repair where storageBlockRequiresRepair:
                let count = storageUnresolvedBlockCount > 0 ? storageUnresolvedBlockCount : storageBlockedCount
                return "Durable upload needs inspection for \(count) source\(count == 1 ? "" : "s")"
            case .repair where isSetupRequired:
                return "Finish setup on this Mac"
            case .repair where isInstallLocationBlocked:
                return "Move Longhouse.app to Applications"
            case .repair:
                return "Local shipping needs repair"
            case .needsUser:
                return "\(needsUser) session\(needsUser == 1 ? "" : "s") need\(needsUser == 1 ? "s" : "") you"
            case .inspect where projectionUnavailable:
                return "Remote session view unavailable"
            case .inspect where storageBlockIsRecovering:
                return "Source upload reconciliation pending for \(storageBlockedCount) source\(storageBlockedCount == 1 ? "" : "s")"
            case .inspect where storageBlockProofUnknown:
                return "Durable upload proof unavailable for \(storageBlockedCount) source\(storageBlockedCount == 1 ? "" : "s")"
            case .inspect where hasDeadLetters:
                return "Durable upload needs inspection for \(deadLetterCount) dead letter\(deadLetterCount == 1 ? "" : "s")"
            case .inspect where reasons.contains("managed_launch_recovery_exhausted"):
                return "Managed session recovery needs attention"
            case .inspect where heartbeatPostFailed:
                return "Machine heartbeat failed"
            case .inspect where heartbeatEvidenceRejected:
                return "Machine evidence rejected"
            case .inspect where transportAttentionReason != nil:
                return "Local upload needs attention"
            case .inspect where sessionDiscoveryAttention:
                return "Session discovery needs attention"
            case .inspect where degraded > 0:
                return "Remote control unavailable for \(degraded) session\(degraded == 1 ? "" : "s")"
            case .inspect where orphanBridgeCount > 0:
                return "\(orphanBridgeCount) background process\(orphanBridgeCount == 1 ? "" : "es") need cleanup"
            case .inspect:
                return "Historical archive needs review"
            case .unavailable where localStatusStale && !localEvidenceUnavailable:
                return "Local status is stale"
            case .unavailable:
                return "Current local status unavailable"
            case .normal where working > 0:
                return "\(working) agent\(working == 1 ? "" : "s") working"
            case .normal where !sessions.isEmpty:
                // "Idle" only when every session is observed idle; missing
                // phase evidence is not idleness.
                let state = idle == sessions.count ? "idle" : "open"
                return "\(sessions.count) session\(sessions.count == 1 ? "" : "s") \(state)"
            case .normal:
                return "No sessions running"
            }
        }
        let systemHeadline: String
        let detail: String?
        if hostUpdateIsSystemState, let hostUpdate {
            systemHeadline = hostUpdate.headline
            detail = hostUpdate.detail
        } else {
            systemHeadline = headlineText(for: systemPromotion)
            detail = nil
        }
        let headline = systemPromotion == promotion ? systemHeadline : headlineText(for: promotion)
        return MenuBarPresentation(
            promotion: promotion,
            headline: headline,
            systemPromotion: systemPromotion,
            systemHeadline: systemHeadline,
            detail: detail,
            hostUpdateClaimIsValid: hasValidUpdateClaim,
            facts: menuBarSystemFacts(
                relativeTo: referenceDate,
                localEvidenceTrust: localEvidenceTrust,
                projectionTrust: projectionTrust,
                hostUpdate: hostUpdate,
                heartbeatPostFailed: heartbeatPostFailed
            ),
            backgroundActivity: archiveBackgroundActivity
        )
    }

    private func menuBarSystemFacts(
        relativeTo referenceDate: Date,
        localEvidenceTrust: DataTrust,
        projectionTrust: DataTrust,
        hostUpdate: HostLinkDisplay?,
        heartbeatPostFailed: Bool
    ) -> [MenuBarSystemFact] {
        let localEvidenceUnavailable = !localEvidenceTrust.isCurrent
        let projectionUnavailable = !projectionTrust.isCurrent
        let deadLetterCount = max(
            engineStatus?.payload?.spoolDeadCount ?? 0,
            reasons.contains("spool_dead") || reasons.contains("spool_dead_letters") ? 1 : 0
        )
        let hasDeadLetters = deadLetterCount > 0
        // Service-manager state is authoritative for process liveness when
        // present. Engine freshness qualifies telemetry, not whether a
        // running service should be called stopped.
        let localAgentRunning: Bool
        if localEvidenceUnavailable {
            localAgentRunning = false
        } else if service != nil {
            localAgentRunning = serviceStatusLabel == "running"
        } else {
            localAgentRunning = engineStatus?.fresh == true && engineStatus?.payload?.daemonPid != nil
        }
        let localValue: String
        if localEvidenceUnavailable {
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
        if !hasEngineEvidence || localEngineEvidenceUnavailable {
            durableValue = "Unknown"
            durablePromotion = .unavailable
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

        var facts = [
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
        if let heartbeat = heartbeatTransport {
            let value: String
            let detail: String?
            let promotion: MenuBarPromotion
            if let hostUpdate {
                value = "Paused · updating"
                detail = hostUpdate.detail
                promotion = hostUpdate.promotion
            } else if localEngineEvidenceUnavailable {
                value = "Unknown"
                detail = "Local status evidence is unavailable"
                promotion = .unavailable
            } else if heartbeatPostFailed {
                value = "POST failed"
                detail = heartbeat.lastError ?? "The Runtime Host has not acknowledged this machine's heartbeat."
                promotion = .inspect
            } else if heartbeatEvidenceRejected {
                value = "Evidence refused"
                detail = "The Runtime Host is reachable, but session evidence was not applied."
                promotion = .inspect
            } else if heartbeat.evidenceState == "applied" {
                value = "Accepted"
                detail = "Machine liveness and session evidence acknowledged"
                promotion = .normal
            } else if heartbeat.state == "healthy" {
                value = "Received"
                detail = "Machine liveness acknowledged; no evidence acceptance reported"
                promotion = .normal
            } else {
                value = "Unknown"
                detail = "No heartbeat acknowledgement yet"
                promotion = .unavailable
            }
            facts.insert(
                MenuBarSystemFact(id: "heartbeat", label: "Status reporting", value: value, detail: detail, promotion: promotion),
                at: facts.count - 1
            )
        } else if let hostUpdate {
            facts.insert(
                MenuBarSystemFact(
                    id: "heartbeat",
                    label: "Status reporting",
                    value: "Paused · updating",
                    detail: hostUpdate.detail,
                    promotion: hostUpdate.promotion
                ),
                at: facts.count - 1
            )
        }
        return facts
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
