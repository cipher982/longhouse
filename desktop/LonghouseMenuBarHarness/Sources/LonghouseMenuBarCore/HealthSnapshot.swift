import Foundation
import SwiftUI

public enum HarnessSeverity: String, Codable, CaseIterable, Sendable {
    case green
    case yellow
    case red
    case gray

    public var accentColor: Color {
        switch self {
        case .green:
            return Color(red: 0.17, green: 0.70, blue: 0.39)
        case .yellow:
            return Color(red: 0.90, green: 0.67, blue: 0.16)
        case .red:
            return Color(red: 0.86, green: 0.29, blue: 0.23)
        case .gray:
            return Color(red: 0.48, green: 0.53, blue: 0.58)
        }
    }

    public var symbolName: String {
        switch self {
        case .green:
            return "checkmark.circle.fill"
        case .yellow:
            return "exclamationmark.triangle.fill"
        case .red:
            return "xmark.circle.fill"
        case .gray:
            return "circle.dotted"
        }
    }

    public var uppercaseLabel: String {
        rawValue.uppercased()
    }
}

public struct HealthSnapshot: Codable, Equatable, Sendable {
    public let schemaVersion: Int?
    public let collectedAt: String?
    public let healthState: String
    public let severity: String
    public let headline: String
    public let reasons: [String]
    public let suggestedActions: [String]
    public let attention: AttentionSnapshot?
    public let service: ServiceSnapshot?
    public let engineStatus: EngineStatusSnapshot?
    public let outbox: OutboxSnapshot?
    public let activitySummary: ActivitySummarySnapshot?
    public let managedSummary: ManagedSummarySnapshot?
    public let managedSessions: [ManagedSessionSnapshot]?
    public let unmanagedProcesses: [UnmanagedProcessSnapshot]?
    public let orphanBridges: [OrphanBridgeSnapshot]?
    public let launchReadiness: LaunchReadinessSnapshot?
    public let build: BuildIdentitySnapshot?
    public let updateInfo: UpdateInfoSnapshot?

    public init(
        schemaVersion: Int?,
        collectedAt: String?,
        healthState: String,
        severity: String,
        headline: String,
        reasons: [String],
        suggestedActions: [String],
        attention: AttentionSnapshot? = nil,
        service: ServiceSnapshot?,
        engineStatus: EngineStatusSnapshot?,
        outbox: OutboxSnapshot?,
        activitySummary: ActivitySummarySnapshot?,
        managedSummary: ManagedSummarySnapshot? = nil,
        managedSessions: [ManagedSessionSnapshot]? = nil,
        unmanagedProcesses: [UnmanagedProcessSnapshot]? = nil,
        orphanBridges: [OrphanBridgeSnapshot]? = nil,
        launchReadiness: LaunchReadinessSnapshot?,
        build: BuildIdentitySnapshot? = nil,
        updateInfo: UpdateInfoSnapshot? = nil
    ) {
        self.schemaVersion = schemaVersion
        self.collectedAt = collectedAt
        self.healthState = healthState
        self.severity = severity
        self.headline = headline
        self.reasons = reasons
        self.suggestedActions = suggestedActions
        self.attention = attention
        self.service = service
        self.engineStatus = engineStatus
        self.outbox = outbox
        self.activitySummary = activitySummary
        self.managedSummary = managedSummary
        self.managedSessions = managedSessions
        self.unmanagedProcesses = unmanagedProcesses
        self.orphanBridges = orphanBridges
        self.launchReadiness = launchReadiness
        self.build = build
        self.updateInfo = updateInfo
    }

    public var parsedSeverity: HarnessSeverity {
        HarnessSeverity(rawValue: severity) ?? .gray
    }

    public var displaySeverity: HarnessSeverity {
        if let managedAttentionSeverity,
           managedAttentionSeverity == .yellow || managedAttentionSeverity == .red {
            return managedAttentionSeverity
        }
        return parsedSeverity
    }

    public var effectiveHeadline: String {
        let attentionHeadline = attention?.headline?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let attentionHeadline, !attentionHeadline.isEmpty {
            return attentionHeadline
        }
        return headline
    }

    public var menuBarAttentionSeverity: HarnessSeverity? {
        guard let attention else {
            switch parsedSeverity {
            case .yellow:
                return .yellow
            case .red:
                return .red
            case .green, .gray:
                return nil
            }
        }

        switch attention.normalizedState {
        case "repair":
            return .red
        case "needs_attention":
            return .yellow
        default:
            return nil
        }
    }

    public var isSetupRequired: Bool {
        if launchReadiness?.state == "setup-required" {
            return true
        }
        return reasons.contains("desktop_app_setup_required")
    }

    public var isInstallLocationBlocked: Bool {
        if launchReadiness?.state == "move-app" {
            return true
        }
        return reasons.contains("desktop_app_wrong_install_location")
    }

    public var statusBadge: String {
        "\(parsedSeverity.uppercaseLabel) · \(healthState.replacingOccurrences(of: "_", with: " ").uppercased())"
    }

    public var ambientStatusLabel: String {
        if isInstallLocationBlocked {
            return "Needs move"
        }
        if isSetupRequired {
            return "Setup required"
        }
        switch parsedSeverity {
        case .green:
            return "Healthy"
        case .yellow:
            return "Watching"
        case .red:
            return "Needs repair"
        case .gray:
            return "Unknown"
        }
    }

    public var needsMenuBarAttention: Bool {
        if managedNeedsAttention {
            return true
        }
        if isInstallLocationBlocked || isSetupRequired {
            return true
        }
        return menuBarAttentionSeverity != nil
    }

    public var lastShipLabel: String {
        engineStatus?.payload?.lastShipAt ?? "No shipments yet"
    }

    public var collectedAtDate: Date? {
        guard let collectedAt else {
            return nil
        }
        return Self.parseISO8601(collectedAt)
    }

    public var snapshotAgeLabel: String {
        snapshotAgeLabel(relativeTo: Date())
    }

    public func snapshotAgeLabel(relativeTo referenceDate: Date) -> String {
        guard let collectedAtDate else {
            return "Unknown"
        }
        return Self.relativeLabel(for: collectedAtDate, relativeTo: referenceDate)
    }

    public var snapshotAgeCompactLabel: String {
        snapshotAgeCompactLabel(relativeTo: Date())
    }

    public func snapshotAgeCompactLabel(relativeTo referenceDate: Date) -> String {
        guard let collectedAtDate else {
            return "Unknown"
        }
        let seconds = max(0, Int(referenceDate.timeIntervalSince(collectedAtDate)))
        return Self.compactAgeLabel(seconds: seconds)
    }

    public var lastShipSummaryLabel: String {
        lastShipSummaryLabel(relativeTo: Date())
    }

    public func lastShipSummaryLabel(relativeTo referenceDate: Date) -> String {
        guard let raw = engineStatus?.payload?.lastShipAt else {
            return "No shipments yet"
        }
        guard let parsed = Self.parseISO8601(raw) else {
            return "Last ship \(raw)"
        }
        return "Last ship \(Self.relativeLabel(for: parsed, relativeTo: referenceDate))"
    }

    public var lastShipValueLabel: String {
        lastShipValueLabel(relativeTo: Date())
    }

    public func lastShipValueLabel(relativeTo referenceDate: Date) -> String {
        guard let raw = engineStatus?.payload?.lastShipAt else {
            return "No shipments yet"
        }
        guard let parsed = Self.parseISO8601(raw) else {
            return raw
        }
        return Self.relativeLabel(for: parsed, relativeTo: referenceDate)
    }

    public var lastShipCompactLabel: String {
        lastShipCompactLabel(relativeTo: Date())
    }

    public func lastShipCompactLabel(relativeTo referenceDate: Date) -> String {
        guard let raw = engineStatus?.payload?.lastShipAt,
              let parsed = Self.parseISO8601(raw) else {
            return "-"
        }
        return Self.compactRelativeLabel(for: parsed, relativeTo: referenceDate)
    }

    public var serviceStatusLabel: String {
        service?.status?.replacingOccurrences(of: "-", with: " ") ?? "unknown"
    }

    public var serviceStatusTitle: String {
        serviceStatusLabel.capitalized
    }

    public var outboxCount: Int {
        outbox?.fileCount ?? 0
    }

    public var outboxOldestLabel: String {
        if let seconds = outbox?.oldestAgeSeconds {
            return Self.ageLabel(seconds: seconds)
        }
        return "-"
    }

    public var engineAgeLabel: String {
        engineAgeLabel(relativeTo: Date())
    }

    public func engineAgeLabel(relativeTo referenceDate: Date) -> String {
        if let seconds = engineStatus?.ageSeconds {
            return Self.ageLabel(seconds: dynamicEngineAgeSeconds(relativeTo: referenceDate, fallback: seconds))
        }
        return "-"
    }

    public var engineFreshnessLabel: String {
        engineFreshnessLabel(relativeTo: Date())
    }

    public func engineFreshnessLabel(relativeTo referenceDate: Date) -> String {
        guard let ageSeconds = engineStatus?.ageSeconds else {
            return "Unknown"
        }
        let dynamicAgeSeconds = dynamicEngineAgeSeconds(relativeTo: referenceDate, fallback: ageSeconds)
        if dynamicAgeSeconds <= 30 {
            return "Fresh"
        }
        if dynamicAgeSeconds <= 120 {
            return "Aging"
        }
        return "Stale"
    }

    public var engineFreshnessValueLabel: String {
        engineFreshnessValueLabel(relativeTo: Date())
    }

    public func engineFreshnessValueLabel(relativeTo referenceDate: Date) -> String {
        guard let ageSeconds = engineStatus?.ageSeconds else {
            return "Unavailable"
        }
        let dynamicAgeSeconds = dynamicEngineAgeSeconds(relativeTo: referenceDate, fallback: ageSeconds)
        return "\(engineFreshnessLabel(relativeTo: referenceDate)) · \(Self.ageLabel(seconds: dynamicAgeSeconds))"
    }

    public var spoolPendingLabel: String {
        String(engineStatus?.payload?.spoolPendingCount ?? 0)
    }

    public var spoolDeadLabel: String {
        String(engineStatus?.payload?.spoolDeadCount ?? 0)
    }

    public var pipelineValueLabel: String {
        let pending = engineStatus?.payload?.spoolPendingCount ?? 0
        let dead = engineStatus?.payload?.spoolDeadCount ?? 0

        if dead > 0 {
            return "\(pending) pending · \(dead) dead"
        }
        if pending > 0 && outboxCount > 0 {
            return "\(pending) pending · \(outboxCount) outbox"
        }
        if pending > 0 {
            return "\(pending) pending"
        }
        if outboxCount > 0 {
            return "\(outboxCount) outbox"
        }
        return "Clear"
    }

    private var shippingBacklogAttentionLabel: String? {
        let pending = engineStatus?.payload?.spoolPendingCount ?? 0
        let outboxFiles = outboxCount
        let dead = engineStatus?.payload?.spoolDeadCount ?? 0

        if dead > 0 {
            return "\(dead) dead queued shipping item\(dead == 1 ? "" : "s") need manual attention before Longhouse can drain the backlog."
        }

        guard pending > 0 || outboxFiles > 0 else {
            return nil
        }

        var parts: [String] = []
        if pending > 0 {
            parts.append("\(pending) queued transcript range\(pending == 1 ? "" : "s")")
        }
        if outboxFiles > 0 {
            parts.append("\(outboxFiles) local hook event\(outboxFiles == 1 ? "" : "s")")
        }
        let backlog = parts.joined(separator: " and ")
        return "Shipping has \(backlog) waiting. Repair now logs each phase: reconcile runtime, replay backlog, then collect health."
    }

    private var shippingFailureAttentionLabel: String? {
        let failures = engineStatus?.payload?.consecutiveShipFailures ?? 0
        guard failures > 0 else {
            return nil
        }
        return "\(failures) consecutive shipping failure\(failures == 1 ? "" : "s") recorded. Repair now shows whether it is replaying data or still failing to connect."
    }

    public var latestActivityLabel: String {
        latestActivityLabel(relativeTo: Date())
    }

    public func latestActivityLabel(relativeTo referenceDate: Date) -> String {
        guard let raw = activitySummary?.latestActivityAt,
              let parsed = Self.parseISO8601(raw) else {
            return "No recent sessions"
        }
        return Self.relativeLabel(for: parsed, relativeTo: referenceDate)
    }

    public var latestActivityCompactLabel: String {
        latestActivityCompactLabel(relativeTo: Date())
    }

    public func latestActivityCompactLabel(relativeTo referenceDate: Date) -> String {
        guard let raw = activitySummary?.latestActivityAt,
              let parsed = Self.parseISO8601(raw) else {
            return "-"
        }
        return Self.compactRelativeLabel(for: parsed, relativeTo: referenceDate)
    }

    public var sessionsTodayLabel: String {
        String(activitySummary?.sessionsToday ?? 0)
    }

    public var sessionsRecentLabel: String {
        String(activitySummary?.sessionsRecent ?? 0)
    }

    public var hotSessionsLabel: String {
        String(sessionRecencyBands.first?.sessionCount ?? 0)
    }

    public var recentWindowLabel: String {
        let minutes = activitySummary?.recentWindowMinutes ?? 15
        return "Last \(minutes)m"
    }

    public var recentWindowCompactLabel: String {
        let minutes = activitySummary?.recentWindowMinutes ?? 15
        return "\(minutes)m"
    }

    public var recentActivitySummaryLabel: String {
        let count = activitySummary?.sessionsRecent ?? 0
        let minutes = activitySummary?.recentWindowMinutes ?? 15
        return count == 1 ? "1 active in \(minutes)m" : "\(count) active in \(minutes)m"
    }

    public var recentTouches: [ActivityTouchSnapshot] {
        (activitySummary?.recentTouches ?? [])
            .filter { !($0.provider ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }

    public var currentManagedSessions: [ManagedSessionSnapshot] {
        managedSessions ?? []
    }

    public var currentUnmanagedProcesses: [UnmanagedProcessSnapshot] {
        unmanagedProcesses ?? []
    }

    public var currentOrphanBridges: [OrphanBridgeSnapshot] {
        orphanBridges ?? []
    }

    public var attachedManagedCount: Int {
        managedSummary?.attachedCount ?? currentManagedSessions.filter { $0.normalizedState == "attached" }.count
    }

    public var detachedManagedCount: Int {
        managedSummary?.detachedCount ?? currentManagedSessions.filter { $0.normalizedState == "detached" }.count
    }

    public var degradedManagedCount: Int {
        managedSummary?.degradedCount ?? currentManagedSessions.filter { $0.normalizedState == "degraded" }.count
    }

    public var orphanBridgeCount: Int {
        managedSummary?.orphanBridgeCount ?? currentOrphanBridges.count
    }

    public var hasManagedRuntimeTruth: Bool {
        managedSummary != nil || !currentManagedSessions.isEmpty || !currentOrphanBridges.isEmpty
    }

    public var managedAttentionSeverity: HarnessSeverity? {
        if currentManagedSessions.contains(where: {
            if case .unknown = $0.menuBarAttentionKind {
                return true
            }
            return false
        }) {
            return .red
        }
        if orphanBridgeCount > 0 || degradedManagedCount > 0 {
            return .red
        }
        if detachedManagedCount > 0 {
            return .yellow
        }
        return nil
    }

    public var managedNeedsAttention: Bool {
        managedAttentionSeverity != nil
    }

    public var managedSummaryLabel: String {
        if !hasManagedRuntimeTruth {
            return "No managed sessions"
        }

        var parts: [String] = []
        if attachedManagedCount > 0 {
            parts.append(Self.countLabel(attachedManagedCount, singular: "attached", plural: "attached"))
        }
        if detachedManagedCount > 0 {
            parts.append(Self.countLabel(detachedManagedCount, singular: "detached", plural: "detached"))
        }
        if degradedManagedCount > 0 {
            parts.append(Self.countLabel(degradedManagedCount, singular: "degraded", plural: "degraded"))
        }
        if orphanBridgeCount > 0 {
            parts.append(Self.countLabel(orphanBridgeCount, singular: "orphan bridge", plural: "orphan bridges"))
        }
        return parts.isEmpty ? "No managed sessions" : parts.joined(separator: " · ")
    }

    public var managedHeaderChipLabel: String? {
        guard hasManagedRuntimeTruth else {
            return nil
        }
        if orphanBridgeCount > 0 {
            return Self.countLabel(orphanBridgeCount, singular: "orphan bridge", plural: "orphan bridges").uppercased()
        }
        if degradedManagedCount > 0 {
            return Self.countLabel(degradedManagedCount, singular: "degraded", plural: "degraded").uppercased()
        }
        if detachedManagedCount > 0 {
            return Self.countLabel(detachedManagedCount, singular: "detached", plural: "detached").uppercased()
        }
        if attachedManagedCount > 0 {
            return Self.countLabel(attachedManagedCount, singular: "attached", plural: "attached").uppercased()
        }
        return "NO MANAGED SESSIONS"
    }

    public var updateAvailableChipLabel: String? {
        guard let updateInfo, updateInfo.shouldNudge else {
            return nil
        }
        if let latest = updateInfo.latestVersion, !latest.isEmpty {
            return "UPDATE \(latest)"
        }
        return "UPDATE AVAILABLE"
    }

    public var installedVersionLabel: String {
        // Qualified build identity ("0.2.0-dev+b672fcca[.dirty]" or
        // "0.2.0 (b672fcca)"). If missing, surface loudly rather than
        // fall back to raw PyPI release version.
        build?.installed?.qualified ?? "BUILD IDENTITY MISSING"
    }

    public var hasResolvedInstalledVersion: Bool {
        build?.installed?.qualified != nil
    }

    /// True when the shipper daemon is running a stale engine binary:
    /// installed vs engine commit_short disagree, or the on-disk engine
    /// binary is newer than the daemon's start time. Benign; needs a
    /// daemon restart.
    public var engineRestartPending: Bool {
        build?.engineRestartPending == true
    }

    /// Soft chip surfaced in the header when a daemon restart is pending.
    /// Intentionally not alarming — normal post-install state, not an error.
    public var restartPendingChipLabel: String? {
        guard engineRestartPending else { return nil }
        return "RESTART PENDING"
    }

    public var statusItemSummaryLabel: String {
        let base = managedNeedsAttention ? "Longhouse needs attention" : "Longhouse \(ambientStatusLabel.lowercased())"
        guard hasManagedRuntimeTruth else {
            return base
        }
        return "\(base) · \(managedSummaryLabel)"
    }

    public func recentTouchAgeLabel(_ touch: ActivityTouchSnapshot, relativeTo referenceDate: Date) -> String {
        guard let raw = touch.lastUpdated,
              let parsed = Self.parseISO8601(raw) else {
            return "-"
        }
        return Self.compactRelativeLabel(for: parsed, relativeTo: referenceDate)
    }

    public func recentTouchWorkspaceLabel(_ touch: ActivityTouchSnapshot) -> String {
        let workspace = (touch.workspaceLabel ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !workspace.isEmpty {
            return workspace
        }
        let provider = (touch.provider ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if provider.isEmpty {
            return "Unknown"
        }
        return Self.providerDisplayName(provider)
    }

    public func recentTouchProviderLabel(_ touch: ActivityTouchSnapshot) -> String {
        let provider = (touch.provider ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if provider.isEmpty {
            return "Unknown"
        }
        return Self.providerDisplayName(provider)
    }

    public func recentTouchTitle(_ touch: ActivityTouchSnapshot) -> String {
        let workspace = (touch.workspaceLabel ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let providerLabel = recentTouchProviderLabel(touch)

        if !workspace.isEmpty {
            if providerLabel == "Unknown" {
                return workspace
            }
            return "\(workspace) · \(providerLabel)"
        }

        return providerLabel
    }

    public var providerCountsToday: [(provider: String, count: Int)] {
        sortedProviderCounts(activitySummary?.providerCountsToday)
    }

    public var providerCountsRecent: [(provider: String, count: Int)] {
        sortedProviderCounts(activitySummary?.providerCountsRecent)
    }

    public var providerMixLabel: String {
        let entries = providerCountsToday
        guard !entries.isEmpty else {
            return "No tracked sessions today"
        }
        return entries
            .map { "\(Self.providerDisplayName($0.provider)) \($0.count)" }
            .joined(separator: " · ")
    }

    public var recentProviderMixLabel: String {
        let entries = providerCountsRecent
        guard !entries.isEmpty else {
            return "No recent provider traffic"
        }
        return entries
            .map { "\(Self.providerDisplayName($0.provider)) \($0.count)" }
            .joined(separator: " · ")
    }

    public var liveUnmanagedSummaryLabel: String {
        let count = currentUnmanagedProcesses.count
        return count == 1 ? "1 live" : "\(count) live"
    }

    public var liveUnmanagedProviderMixLabel: String {
        let counts = currentUnmanagedProcesses.reduce(into: [String: Int]()) { partialResult, process in
            let provider = (process.provider ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !provider.isEmpty else {
                return
            }
            partialResult[provider, default: 0] += 1
        }
        let entries = sortedProviderCounts(counts)
        guard !entries.isEmpty else {
            return "No live unmanaged sessions"
        }
        return entries
            .map { "\(Self.providerDisplayName($0.provider)) \($0.count)" }
            .joined(separator: " · ")
    }

    public var sessionRecencyBands: [ActivityRecencyBandSnapshot] {
        (activitySummary?.sessionRecencyBands ?? [])
            .filter { !$0.label.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }

    public var diskFreeLabel: String {
        guard let bytes = engineStatus?.payload?.diskFreeBytes else {
            return "-"
        }
        return ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .binary)
    }

    public var diskFreeCompactLabel: String {
        guard let bytes = engineStatus?.payload?.diskFreeBytes else {
            return "-"
        }
        let gib = Double(bytes) / Double(1024 * 1024 * 1024)
        return "\(Int(gib.rounded()))G"
    }

    public var parseErrorCountLabel: String {
        String(engineStatus?.payload?.parseErrorCount1H ?? 0)
    }

    public var consecutiveFailuresLabel: String {
        String(engineStatus?.payload?.consecutiveShipFailures ?? 0)
    }

    public var launchStateLabel: String {
        launchReadiness?.state ?? "-"
    }

    public var launchSummaryLabel: String {
        let state = launchStateLabel
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if state.lowercased() == "ready",
           let machineName = launchReadiness?.machineName,
           !machineName.isEmpty {
            return "Launch configured on \(machineName)"
        }
        if state.isEmpty || state == "-" {
            return "Launch state unavailable"
        }
        return "Launch \(state)"
    }

    public var launchValueLabel: String {
        let state = launchStateLabel
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)

        if state.lowercased() == "ready",
           let machineName = launchReadiness?.machineName,
           !machineName.isEmpty {
            return "Configured on \(machineName)"
        }

        if state.isEmpty || state == "-" {
            return "Unavailable"
        }

        return state.prefix(1).uppercased() + state.dropFirst()
    }

    public var runnerNameValueLabel: String {
        if let runnerName = launchReadiness?.runner?.runnerName,
           !runnerName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return runnerName
        }
        if let machineName = launchReadiness?.machineName,
           !machineName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return machineName
        }
        return "-"
    }

    public var installModeValueLabel: String {
        let raw = launchReadiness?.runner?.installMode ?? "unknown"
        return raw
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
            .capitalized
    }

    public var hostValueLabel: String {
        let rawURL = launchReadiness?.storedURL ?? launchReadiness?.runner?.runnerURLs?.first
        guard let rawURL,
              let parsedURL = URL(string: rawURL),
              let host = parsedURL.host,
              !host.isEmpty else {
            return "-"
        }
        if let shortHost = host.split(separator: ".").first, !shortHost.isEmpty {
            return String(shortHost)
        }
        return host
    }

    public var machineNameLabel: String {
        let machineName = launchReadiness?.machineName?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let machineName, !machineName.isEmpty {
            return machineName
        }
        return "Unknown"
    }

    public var pipelineSentenceLabel: String {
        switch pipelineValueLabel {
        case "Clear":
            return "clear"
        default:
            return pipelineValueLabel.lowercased()
        }
    }

    public var missionSummaryLabel: String {
        missionSummaryLabel(relativeTo: Date())
    }

    public func missionSummaryLabel(relativeTo referenceDate: Date) -> String {
        if isInstallLocationBlocked {
            return "Longhouse.app must live in /Applications"
        }
        let lastShipCompact = lastShipCompactLabel(relativeTo: referenceDate)
        let shipLead = lastShipCompact == "-" ? "Ship \(lastShipValueLabel(relativeTo: referenceDate))" : "Ship \(lastShipCompact)"
        var parts = [shipLead]
        let recent = activitySummary?.sessionsRecent ?? 0
        if recent > 0 {
            parts.append("\(recent) active")
        }
        if hasManagedRuntimeTruth {
            parts.append(managedSummaryLabel)
        }
        if launchValueLabel != "Unavailable" {
            parts.append(launchValueLabel)
        }
        return parts.joined(separator: " · ")
    }

    public var attentionSummaryLabel: String {
        if isInstallLocationBlocked {
            return "Longhouse.app only runs from /Applications. Quit, move the app there, then relaunch."
        }
        if isSetupRequired {
            return "Longhouse.app needs to finish setup on this Mac. Set Up Longhouse to install the CLI, runtime, and menu bar wiring."
        }
        let primaryReason = reasons.first.map(Self.humanizeReason)
        switch parsedSeverity {
        case .green:
            return "Shipping is healthy on this Mac. Leave Longhouse running quietly in the menu bar."
        case .yellow:
            if attention?.normalizedState == "watching",
               let summary = attention?.summary?.trimmingCharacters(in: .whitespacesAndNewlines),
               !summary.isEmpty {
                return summary
            }
            if let shippingBacklogAttentionLabel {
                return shippingBacklogAttentionLabel
            }
            if let shippingFailureAttentionLabel {
                return shippingFailureAttentionLabel
            }
            if let primaryReason {
                return "\(primaryReason). Refresh or inspect logs if this keeps aging."
            }
            if let summary = attention?.summary?.trimmingCharacters(in: .whitespacesAndNewlines),
               !summary.isEmpty {
                return summary
            }
            return "Longhouse is still shipping, but local status is aging."
        case .red:
            if let shippingBacklogAttentionLabel {
                return shippingBacklogAttentionLabel
            }
            if let shippingFailureAttentionLabel {
                return shippingFailureAttentionLabel
            }
            if let primaryReason {
                return "\(primaryReason). Repair is the fastest path to restore shipping."
            }
            if let summary = attention?.summary?.trimmingCharacters(in: .whitespacesAndNewlines),
               !summary.isEmpty {
                return summary
            }
            return "Shipping is blocked on this Mac. Repair is the fastest path to restore it."
        case .gray:
            return "Longhouse could not determine the current local health state."
        }
    }

    public var machineRunnerLabel: String {
        let machineName = launchReadiness?.machineName ?? "-"
        let runnerName = launchReadiness?.runner?.runnerName ?? "-"
        return "\(machineName) / \(runnerName)"
    }

    public var serviceMachineLabel: String {
        launchReadiness?.serviceMachineName ?? "-"
    }

    public var storedRunnerURLLabel: String {
        let storedURL = launchReadiness?.storedURL ?? "-"
        let runnerURL = launchReadiness?.runner?.runnerURLs?.joined(separator: ", ") ?? "-"
        return "\(storedURL) / \(runnerURL)"
    }

    private static func ageLabel(seconds: Int) -> String {
        if seconds < 60 {
            return "\(seconds)s"
        }
        if seconds < 3600 {
            return "\(seconds / 60)m"
        }
        return "\(seconds / 3600)h"
    }

    private static func compactAgeLabel(seconds: Int) -> String {
        if seconds < 60 {
            return "\(seconds)s"
        }
        if seconds < 3600 {
            return "\(seconds / 60)m"
        }
        if seconds < 86_400 {
            return "\(seconds / 3600)h"
        }
        return "\(seconds / 86_400)d"
    }

    private func collectionElapsedSeconds(relativeTo referenceDate: Date) -> Int {
        guard let collectedAtDate else {
            return 0
        }
        return max(0, Int(referenceDate.timeIntervalSince(collectedAtDate)))
    }

    private func sortedProviderCounts(_ providerCounts: [String: Int]?) -> [(provider: String, count: Int)] {
        guard let providerCounts,
              !providerCounts.isEmpty else {
            return []
        }

        let preferredOrder = ["claude", "codex", "gemini"]
        return providerCounts
            .filter { !$0.key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && $0.value > 0 }
            .sorted { lhs, rhs in
                if lhs.value != rhs.value {
                    return lhs.value > rhs.value
                }
                let lhsIndex = preferredOrder.firstIndex(of: lhs.key.lowercased()) ?? preferredOrder.count
                let rhsIndex = preferredOrder.firstIndex(of: rhs.key.lowercased()) ?? preferredOrder.count
                if lhsIndex != rhsIndex {
                    return lhsIndex < rhsIndex
                }
                return lhs.key.localizedCaseInsensitiveCompare(rhs.key) == .orderedAscending
            }
            .map { ($0.key, $0.value) }
    }

    private func dynamicEngineAgeSeconds(relativeTo referenceDate: Date, fallback: Int) -> Int {
        max(0, fallback + collectionElapsedSeconds(relativeTo: referenceDate))
    }

    private static func compactRelativeLabel(for date: Date, relativeTo referenceDate: Date) -> String {
        compactAgeLabel(seconds: max(0, Int(referenceDate.timeIntervalSince(date))))
    }

    internal static func parseISO8601(_ raw: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let parsed = fractional.date(from: raw) {
            return parsed
        }

        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        return plain.date(from: raw)
    }

    private static func relativeLabel(for date: Date, relativeTo referenceDate: Date) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .full
        return formatter.localizedString(for: date, relativeTo: referenceDate)
    }

    public func relativeTimestampLabel(_ raw: String?, relativeTo referenceDate: Date) -> String {
        guard let raw,
              let parsed = Self.parseISO8601(raw) else {
            return "Unknown"
        }
        return Self.relativeLabel(for: parsed, relativeTo: referenceDate)
    }

    public func compactTimestampLabel(_ raw: String?, relativeTo referenceDate: Date) -> String {
        guard let raw,
              let parsed = Self.parseISO8601(raw) else {
            return "-"
        }
        return Self.compactRelativeLabel(for: parsed, relativeTo: referenceDate)
    }

    static func providerDisplayName(_ raw: String) -> String {
        switch raw.lowercased() {
        case "claude":
            return "Claude"
        case "codex":
            return "Codex"
        case "gemini":
            return "Gemini"
        default:
            return raw.capitalized
        }
    }

    private static func humanizeReason(_ raw: String) -> String {
        switch raw {
        case "desktop_app_setup_required":
            return "Longhouse needs setup on this Mac"
        case "desktop_app_wrong_install_location":
            return "Longhouse.app is not in /Applications"
        case "managed_unknown_phase":
            return "Longhouse saw an unknown managed phase"
        case "service_stopped":
            return "The local service is stopped"
        case "spool_dead":
            return "Dead letters need attention"
        case "outbox_stuck":
            return "The outbox is backing up"
        case "engine_status_stale":
            return "The engine status is stale"
        default:
            return raw
                .replacingOccurrences(of: "_", with: " ")
                .replacingOccurrences(of: "-", with: " ")
                .capitalized
        }
    }

    public static func humanizeManagedReason(_ raw: String) -> String {
        switch raw {
        case "no_managed_session_bound":
            return "No managed session bound"
        case "bridge_heartbeat_stale":
            return "Bridge heartbeat stale"
        case "thread_subscription_failed":
            return "Transcript shipping stalled"
        case "live_control_unavailable":
            return "Control path unavailable"
        default:
            return humanizeReason(raw)
        }
    }

    private static func countLabel(_ count: Int, singular: String, plural: String? = nil) -> String {
        if count == 1 {
            return "1 \(singular)"
        }
        return "\(count) \(plural ?? "\(singular)s")"
    }

    public static func setupRequiredSnapshot(detail: String? = nil) -> HealthSnapshot {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let suggestedAction = "Set up Longhouse from this app to install the CLI, runtime, and menu bar service."
        let reason = detail?.trimmingCharacters(in: .whitespacesAndNewlines)

        return HealthSnapshot(
            schemaVersion: 1,
            collectedAt: formatter.string(from: Date()),
            healthState: "broken",
            severity: "red",
            headline: "Longhouse setup required",
            reasons: ["desktop_app_setup_required"],
            suggestedActions: [suggestedAction],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: LaunchReadinessSnapshot(
                state: "setup-required",
                headline: "Longhouse setup required",
                reasons: reason.map { [$0] } ?? ["Longhouse CLI is not installed yet."],
                suggestedActions: [suggestedAction],
                storedURL: nil,
                machineName: nil,
                serviceMachineName: nil,
                runner: nil
            )
        )
    }

    public static func installLocationBlockedSnapshot(
        currentPath: String,
        canonicalPath: String = "/Applications/Longhouse.app"
    ) -> HealthSnapshot {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let normalizedCurrentPath = currentPath.trimmingCharacters(in: .whitespacesAndNewlines)
        let suggestedAction = "Quit Longhouse, move Longhouse.app to \(canonicalPath), then relaunch."

        return HealthSnapshot(
            schemaVersion: 1,
            collectedAt: formatter.string(from: Date()),
            healthState: "broken",
            severity: "red",
            headline: "Move Longhouse.app to Applications",
            reasons: ["desktop_app_wrong_install_location"],
            suggestedActions: [suggestedAction],
            service: nil,
            engineStatus: nil,
            outbox: nil,
            activitySummary: nil,
            launchReadiness: LaunchReadinessSnapshot(
                state: "move-app",
                headline: "Longhouse.app must live in /Applications",
                reasons: [
                    "Current path: \(normalizedCurrentPath)",
                    "Supported path: \(canonicalPath)",
                ],
                suggestedActions: [suggestedAction],
                storedURL: nil,
                machineName: nil,
                serviceMachineName: nil,
                runner: nil
            )
        )
    }
}

public struct ServiceSnapshot: Codable, Equatable, Sendable {
    public let platform: String?
    public let status: String?
    public let serviceName: String?
    public let serviceFile: String?
    public let logPath: String?
}

public struct EngineStatusSnapshot: Codable, Equatable, Sendable {
    public let path: String?
    public let exists: Bool?
    public let fresh: Bool?
    public let ageSeconds: Int?
    public let payload: EngineStatusPayload?
    public let error: String?
}

/// Build identity block emitted by `longhouse local-health --json`.
public struct BuildIdentitySnapshot: Codable, Equatable, Sendable {
    public let installed: BuildIdentityRecord?
    public let engine: BuildIdentityRecord?
    public let engineRestartPending: Bool?

    public init(
        installed: BuildIdentityRecord?,
        engine: BuildIdentityRecord?,
        engineRestartPending: Bool?
    ) {
        self.installed = installed
        self.engine = engine
        self.engineRestartPending = engineRestartPending
    }
}

public struct BuildIdentityRecord: Codable, Equatable, Sendable {
    public let version: String?
    public let commit: String?
    public let commitShort: String?
    public let dirty: Bool?
    public let builtAt: String?
    public let channel: String?
    /// Set by the Python loader when identity is absent or unreadable.
    public let error: String?
    public let detail: String?

    public var qualified: String? {
        guard let version, let commitShort else { return nil }
        if channel == "release" {
            return "\(version) (\(commitShort))"
        }
        let suffix = (dirty == true) ? "\(commitShort).dirty" : commitShort
        return "\(version)-dev+\(suffix)"
    }
}

public struct BuildIdentityComponent: Codable, Equatable, Sendable {
    public let name: String
    public let commitShort: String
}

public struct EngineStatusPayload: Codable, Equatable, Sendable {
    public let version: String?
    public let daemonPid: Int?
    public let lastShipAt: String?
    public let spoolPendingCount: Int?
    public let spoolDeadCount: Int?
    public let parseErrorCount1H: Int?
    public let consecutiveShipFailures: Int?
    public let diskFreeBytes: UInt64?
    public let isOffline: Bool?
    public let recentDeadLetters: [DeadLetterSnapshot]?
    public let lastUpdated: String?
    /// Engine-compiled build identity (from build.rs). Same shape as the
    /// CLI and wheel identity records.
    public let build: BuildIdentityRecord?

    public init(
        version: String?,
        daemonPid: Int?,
        lastShipAt: String?,
        spoolPendingCount: Int?,
        spoolDeadCount: Int?,
        parseErrorCount1H: Int?,
        consecutiveShipFailures: Int?,
        diskFreeBytes: UInt64?,
        isOffline: Bool?,
        recentDeadLetters: [DeadLetterSnapshot]?,
        lastUpdated: String?,
        build: BuildIdentityRecord? = nil
    ) {
        self.version = version
        self.daemonPid = daemonPid
        self.lastShipAt = lastShipAt
        self.spoolPendingCount = spoolPendingCount
        self.spoolDeadCount = spoolDeadCount
        self.parseErrorCount1H = parseErrorCount1H
        self.consecutiveShipFailures = consecutiveShipFailures
        self.diskFreeBytes = diskFreeBytes
        self.isOffline = isOffline
        self.recentDeadLetters = recentDeadLetters
        self.lastUpdated = lastUpdated
        self.build = build
    }

    enum CodingKeys: String, CodingKey {
        case version
        case daemonPid
        case lastShipAt
        case spoolPendingCount
        case spoolDeadCount
        case parseErrorCount1H = "parseErrorCount1h"
        case consecutiveShipFailures
        case diskFreeBytes
        case isOffline
        case recentDeadLetters
        case lastUpdated
        case build
    }
}

public struct DeadLetterSnapshot: Codable, Equatable, Sendable {
    public let provider: String?
    public let filePath: String?
    public let rangeBytes: Int?
    public let createdAt: String?
}

public struct OutboxSnapshot: Codable, Equatable, Sendable {
    public let path: String?
    public let fileCount: Int?
    public let oldestAgeSeconds: Int?
}

public struct UpdateInfoSnapshot: Codable, Equatable, Sendable {
    public let installedVersion: String?
    public let latestVersion: String?
    public let updateAvailable: Bool?
    public let upgradeCommand: String?
    public let checkedAt: String?
    public let supported: Bool?
    public let reason: String?

    public var shouldNudge: Bool {
        (updateAvailable ?? false) && (supported ?? false)
    }
}

public struct AttentionSnapshot: Codable, Equatable, Sendable {
    public let state: String?
    public let headline: String?
    public let summary: String?
    public let reasons: [String]?
    public let suggestedActions: [String]?

    public init(
        state: String?,
        headline: String? = nil,
        summary: String? = nil,
        reasons: [String]? = nil,
        suggestedActions: [String]? = nil
    ) {
        self.state = state
        self.headline = headline
        self.summary = summary
        self.reasons = reasons
        self.suggestedActions = suggestedActions
    }

    public var normalizedState: String {
        let normalized = state?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if let normalized, !normalized.isEmpty {
            return normalized
        }
        return "quiet"
    }
}

public struct ManagedSummarySnapshot: Codable, Equatable, Sendable {
    public let attachedCount: Int?
    public let detachedCount: Int?
    public let degradedCount: Int?
    public let orphanBridgeCount: Int?
    public let latestActivityAt: String?
}

public struct ManagedSessionSnapshot: Codable, Equatable, Identifiable, Sendable {
    public let sessionId: String?
    public let provider: String?
    public let workspaceLabel: String?
    public let branch: String?
    public let state: String?
    public let phase: String?
    public let rawPhase: String?
    public let phaseObservedAt: String?
    public let lastActivityAt: String?
    public let bridgeStatus: String?
    public let bridgePid: Int?
    public let bridgeHeartbeatAt: String?
    public let reasonCodes: [String]?

    public init(
        sessionId: String?,
        provider: String?,
        workspaceLabel: String?,
        branch: String?,
        state: String?,
        phase: String?,
        rawPhase: String? = nil,
        phaseObservedAt: String? = nil,
        lastActivityAt: String?,
        bridgeStatus: String?,
        bridgePid: Int?,
        bridgeHeartbeatAt: String?,
        reasonCodes: [String]?
    ) {
        self.sessionId = sessionId
        self.provider = provider
        self.workspaceLabel = workspaceLabel
        self.branch = branch
        self.state = state
        self.phase = phase
        self.rawPhase = rawPhase
        self.phaseObservedAt = phaseObservedAt
        self.lastActivityAt = lastActivityAt
        self.bridgeStatus = bridgeStatus
        self.bridgePid = bridgePid
        self.bridgeHeartbeatAt = bridgeHeartbeatAt
        self.reasonCodes = reasonCodes
    }

    public var id: String {
        sessionId ?? "\(provider ?? "unknown")-\(workspaceLabel ?? "workspace")-\(lastActivityAt ?? "never")"
    }

    public var normalizedState: String {
        let normalized = state?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if let normalized, !normalized.isEmpty {
            return normalized
        }
        return "unknown"
    }

    var menuBarAttentionKind: ManagedAttentionKind {
        switch normalizedState {
        case "detached":
            return .detached
        case "degraded":
            return .degraded
        case "attached":
            let normalizedPhase = phase?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if normalizedPhase.isEmpty {
                return .idle
            }
            return ManagedPhaseContract.attention(forDisplayPhase: phase) ?? .unknown("unknown phase")
        default:
            return .unknown(normalizedState)
        }
    }
}

public struct OrphanBridgeSnapshot: Codable, Equatable, Identifiable, Sendable {
    public let sessionId: String?
    public let provider: String?
    public let pid: Int?
    public let workspaceLabel: String?
    public let status: String?
    public let startedAt: String?
    public let heartbeatAt: String?
    public let reasonCodes: [String]?

    public var id: String {
        if let pid {
            return "bridge-\(pid)"
        }
        return "\(provider ?? "unknown")-\(workspaceLabel ?? "workspace")-\(startedAt ?? "unknown")"
    }
}

public struct ActivitySummarySnapshot: Codable, Equatable, Sendable {
    public let path: String?
    public let exists: Bool?
    public let error: String?
    public let sessionsToday: Int?
    public let sessionsRecent: Int?
    public let providerCountsToday: [String: Int]?
    public let providerCountsRecent: [String: Int]?
    public let sessionRecencyBands: [ActivityRecencyBandSnapshot]?
    public let recentTouches: [ActivityTouchSnapshot]?
    public let latestActivityAt: String?
    public let recentWindowMinutes: Int?
}

public struct ActivityRecencyBandSnapshot: Codable, Equatable, Sendable {
    public let label: String
    public let sessionCount: Int?
}

public struct ActivityTouchSnapshot: Codable, Equatable, Sendable {
    public let provider: String?
    public let lastUpdated: String?
    public let workspaceLabel: String?
    public let branch: String?
    public let isSubagent: Bool?
}

public struct UnmanagedProcessSnapshot: Codable, Equatable, Identifiable, Sendable {
    public let provider: String?
    public let pid: Int?
    public let workspaceLabel: String?
    public let cwd: String?
    public let branch: String?
    public let startedAt: String?

    public var id: String {
        if let pid {
            return "unmanaged-\(pid)"
        }
        return "\(provider ?? "unknown")-\(workspaceLabel ?? "workspace")-\(startedAt ?? "unknown")"
    }
}

public struct LaunchReadinessSnapshot: Codable, Equatable, Sendable {
    public let state: String?
    public let headline: String?
    public let reasons: [String]?
    public let suggestedActions: [String]?
    public let storedURL: String?
    public let machineName: String?
    public let serviceMachineName: String?
    public let runner: RunnerSnapshot?

    enum CodingKeys: String, CodingKey {
        case state
        case headline
        case reasons
        case suggestedActions
        case storedURL = "storedUrl"
        case machineName
        case serviceMachineName
        case runner
    }
}

public struct RunnerSnapshot: Codable, Equatable, Sendable {
    public let path: String?
    public let exists: Bool?
    public let error: String?
    public let runnerName: String?
    public let runnerID: String?
    public let runnerURLs: [String]?
    public let installMode: String?

    enum CodingKeys: String, CodingKey {
        case path
        case exists
        case error
        case runnerName
        case runnerID = "runnerId"
        case runnerURLs = "runnerUrls"
        case installMode
    }
}
