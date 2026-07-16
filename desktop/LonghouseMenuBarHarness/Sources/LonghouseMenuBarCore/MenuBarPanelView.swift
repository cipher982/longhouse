import SwiftUI

public enum MenuBarPanelLayout {
    public static let panelWidth: CGFloat = 376
    public static let defaultWindowHeight: CGFloat = 560
    public static let chromeCornerRadius: CGFloat = 13
    public static let chromeHorizontalPadding: CGFloat = 14
    public static let chromeBottomPadding: CGFloat = 14
    public static let chromeTopRailInset: CGFloat = 11
    public static let chromeTopContentInset: CGFloat = 26
    public static let accentHorizontalInset: CGFloat = 16
    public static let accentHeight: CGFloat = 2
    public static let rootSpacing: CGFloat = 12
    public static let sectionSpacing: CGFloat = 10
    public static let sectionHeaderSpacing: CGFloat = 9
    public static let sectionInsets = EdgeInsets(top: 10, leading: 11, bottom: 10, trailing: 11)
}

public struct MenuBarLoadingView: View {
    public init() {}

    public var body: some View {
        PanelChrome(accent: .gray) {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .center, spacing: 12) {
                    statusEmblem(color: .gray, systemImage: "arrow.trianglehead.clockwise")

                    VStack(alignment: .leading, spacing: 3) {
                        Text("Refreshing Longhouse")
                            .font(.system(size: 18, weight: .semibold))
                            .foregroundStyle(Color.primary)
                        Text("Longhouse is collecting the latest status for this Mac.")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(Color.secondary)
                    }
                }

                PanelSection(title: "Snapshot") {
                    HStack(spacing: 10) {
                        ProgressView()
                            .controlSize(.small)
                        Text("Loading local runtime status")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(Color.primary)
                    }
                }
            }
        }
    }
}

public struct MenuBarBootingView: View {
    public init() {}

    public var body: some View {
        PanelChrome(accent: .blue) {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .center, spacing: 12) {
                    ZStack {
                        Circle()
                            .fill(Color.blue.opacity(0.14))
                            .frame(width: 34, height: 34)
                        ProgressView()
                            .controlSize(.small)
                    }

                    VStack(alignment: .leading, spacing: 3) {
                        Text("Starting Longhouse")
                            .font(.system(size: 18, weight: .semibold))
                            .foregroundStyle(Color.primary)
                        Text("Bringing up the local engine and checking status.")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(Color.secondary)
                    }
                }

                PanelSection(title: "Startup") {
                    HStack(spacing: 10) {
                        ProgressView()
                            .controlSize(.small)
                        Text("This usually takes a few seconds on first launch.")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(Color.primary)
                    }
                }
            }
        }
    }
}

public struct MenuBarSettlingView: View {
    public init() {}

    public var body: some View {
        PanelChrome(accent: .blue) {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .center, spacing: 12) {
                    ZStack {
                        Circle()
                            .fill(Color.blue.opacity(0.14))
                            .frame(width: 34, height: 34)
                        ProgressView()
                            .controlSize(.small)
                    }

                    VStack(alignment: .leading, spacing: 3) {
                        Text("Catching Up Longhouse")
                            .font(.system(size: 18, weight: .semibold))
                            .foregroundStyle(Color.primary)
                        Text("The local engine is refreshing after an idle gap.")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(Color.secondary)
                    }
                }

                PanelSection(title: "Status") {
                    HStack(spacing: 10) {
                        ProgressView()
                            .controlSize(.small)
                        Text("Warnings appear if status keeps aging.")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(Color.primary)
                    }
                }
            }
        }
    }
}

public struct MenuBarFailureView: View {
    private let message: String
    private let retry: () -> Void

    public init(message: String, retry: @escaping () -> Void) {
        self.message = message
        self.retry = retry
    }

    public var body: some View {
        PanelChrome(accent: .red) {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .center, spacing: 12) {
                    statusEmblem(color: .red, systemImage: "xmark.circle.fill")
                        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Error.headline)
                        .accessibilityLabel(Text("Longhouse could not load desktop status"))

                    VStack(alignment: .leading, spacing: 3) {
                        Text("Longhouse status unavailable")
                            .font(.system(size: 18, weight: .semibold))
                            .foregroundStyle(Color.primary)
                        Text("Longhouse.app could not load its latest status.")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(Color.secondary)
                    }
                }

                PanelSection(title: "Failure") {
                    Text(message)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Color.primary)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Error.message)
                }

                Button(action: retry) {
                    Label("Retry", systemImage: "arrow.clockwise")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Error.retryButton)
                .accessibilityLabel(Text("Retry"))
            }
        }
    }
}

public struct MenuBarPanelView: View {
    private let snapshot: HealthSnapshot
    private let history: [SnapshotHistorySample]
    private let presentationDate: Date
    private let feedback: HealthActionFeedback?
    private let setFeedback: (HealthActionFeedback?) -> Void
    private let actionSink: any HealthActionSink
    private let isManualRefreshing: Bool
    private let refresh: () -> Void
    private let headerSummaryVariant: HeaderSummaryVariant

    public init(
        snapshot: HealthSnapshot,
        history: [SnapshotHistorySample],
        presentationDate: Date,
        feedback: HealthActionFeedback?,
        setFeedback: @escaping (HealthActionFeedback?) -> Void,
        actionSink: any HealthActionSink,
        isManualRefreshing: Bool,
        headerSummaryVariant: HeaderSummaryVariant = .default,
        refresh: @escaping () -> Void
    ) {
        self.snapshot = snapshot
        self.history = history
        self.presentationDate = presentationDate
        self.feedback = feedback
        self.setFeedback = setFeedback
        self.actionSink = actionSink
        self.isManualRefreshing = isManualRefreshing
        self.headerSummaryVariant = headerSummaryVariant
        self.refresh = refresh
    }

    public var body: some View {
        PanelChrome(accent: presentation.promotion.accentColor) {
            VStack(alignment: .leading, spacing: MenuBarPanelLayout.rootSpacing) {
                header
                primarySurface

                if let feedback {
                    feedbackBanner(feedback)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.panel)
    }

    private var presentation: MenuBarPresentation {
        snapshot.menuBarPresentation(relativeTo: presentationDate)
    }

    private var isHealthy: Bool {
        snapshot.parsedSeverity == .green && snapshot.healthState.lowercased() == "healthy"
    }

    /// Yellow is usually a transient condition that Longhouse is already
    /// watching or repairing. Keep that state calm and task-oriented; reserve
    /// the diagnostic table and repair controls for states that need action.
    private var usesCompactWatchingSurface: Bool {
        snapshot.parsedSeverity == .yellow
            && !snapshot.isSetupRequired
            && !snapshot.isInstallLocationBlocked
    }

    private var displayHeadline: String {
        presentation.headline
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            longhouseBrandEmblem(severity: presentation.promotion.iconSeverity)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Header.statusGlyph)

            VStack(alignment: .leading, spacing: 8) {
                Text("LONGHOUSE")
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundStyle(Color.secondary)
                    .tracking(0.9)

                Text(displayHeadline)
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundStyle(Color.primary)
                    .lineLimit(2)
                    .minimumScaleFactor(0.82)
                    .harnessAccessibility(
                        identifier: LonghouseMenuBarAccessibilityID.Header.headline,
                        label: displayHeadline
                    )

                headerSummaryBlock
            }

            Spacer(minLength: 0)

            headerControlGroup
        }
    }

    @ViewBuilder
    private var headerSummaryBlock: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                if presentation.promotion != .normal {
                    headerSummaryStatusPill(
                        title: presentation.promotion.statusLabel.uppercased(),
                        color: presentation.promotion.accentColor,
                        identifier: LonghouseMenuBarAccessibilityID.Header.statusBadge
                    )
                }
                headerSummaryLabel(presentation.subheadline)
            }

            if let updateChip = snapshot.updateAvailableChipLabel {
                subtleChip(title: updateChip, tint: .yellow)
            }

            if let restartChip = snapshot.restartPendingChipLabel {
                subtleChip(title: restartChip, tint: .yellow)
            }
        }
    }

    private var headerMinimalSummary: some View {
        HStack(spacing: 8) {
            headerSummaryStatusPill(
                title: snapshot.ambientStatusLabel.uppercased(),
                color: snapshot.parsedSeverity.accentColor,
                identifier: LonghouseMenuBarAccessibilityID.Header.statusBadge
            )

            headerSummaryLabel("Updated \(snapshot.snapshotAgeCompactLabel(relativeTo: presentationDate))")
        }
    }

    private var headerTelemetryRailSummary: some View {
        HeaderTelemetryRail(
            statusTitle: snapshot.ambientStatusLabel.uppercased(),
            statusColor: snapshot.parsedSeverity.accentColor,
            updatedLabel: snapshot.snapshotAgeCompactLabel(relativeTo: presentationDate),
            metrics: headerTelemetryItems,
            statusIdentifier: LonghouseMenuBarAccessibilityID.Header.statusBadge
        )
    }

    private var headerSessionRibbonSummary: some View {
        HeaderSessionRibbon(
            statusTitle: snapshot.ambientStatusLabel.uppercased(),
            statusColor: snapshot.parsedSeverity.accentColor,
            updatedLabel: snapshot.snapshotAgeCompactLabel(relativeTo: presentationDate),
            tokens: headerSessionTokens,
            managedSummary: snapshot.managedSummaryLabel,
            statusIdentifier: LonghouseMenuBarAccessibilityID.Header.statusBadge
        )
    }

    private var headerControlGroup: some View {
        HStack(spacing: 6) {
            headerAccessoryButton(
                systemImage: "arrow.up.forward.square",
                accessibilityIdentifier: LonghouseMenuBarAccessibilityID.Button.openLonghouse,
                accessibilityLabel: "Open Longhouse"
            ) {
                perform(.openLonghouse)
            }

            healthyToolsMenu

            refreshControl
        }
    }

    private var primarySurface: some View {
        VStack(alignment: .leading, spacing: 0) {
            managedRuntimeSurface

            if !unmanagedActivityEntries.isEmpty {
                sectionDivider.padding(.horizontal, 4)
                PanelSection(title: "Other agent processes", trailing: snapshot.liveUnmanagedSummaryLabel) {
                    UnmanagedActivityList(entries: unmanagedActivityEntries)
                }
            }

            sectionDivider.padding(.horizontal, 4)
            systemFactsSection

            if let backgroundActivity = presentation.backgroundActivity {
                sectionDivider.padding(.horizontal, 4)
                PanelSection(title: "Background activity") {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Image(systemName: "clock.arrow.circlepath")
                            .foregroundStyle(Color.secondary)
                        Text(backgroundActivity)
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(Color.primary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Text("Current sessions and durable uploads have priority.")
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(Color.secondary)
                }
            }

            if presentation.promotion == .repair {
                sectionDivider.padding(.horizontal, 4)
                PanelSection(title: "Action required") {
                    Text(repairGuidance)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Color.primary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                watchingActions
            }
        }
    }

    private var repairGuidance: String {
        if snapshot.storageBlockedCount > 0 {
            return "Local source evidence is retained. Inspect the source conflict before retrying or discarding it."
        }
        if snapshot.isSetupRequired {
            return "Finish setup to install the local agent and connect this Mac."
        }
        if snapshot.isInstallLocationBlocked {
            return "Move Longhouse.app to /Applications, then reopen it."
        }
        return "Current local evidence shows a broken product promise. Open Logs for the exact failing fact."
    }

    private var systemFactsSection: some View {
        PanelSection(title: "System facts") {
            TelemetryTable(entries: presentation.facts.map { fact in
                PanelTelemetryEntry(
                    id: fact.id,
                    label: fact.label,
                    value: [fact.value, fact.detail].compactMap { $0 }.joined(separator: " · "),
                    valueColor: fact.promotion.accentColor
                )
            })
        }
    }

    private var refreshControl: some View {
        headerAccessoryButton(
            accessibilityIdentifier: LonghouseMenuBarAccessibilityID.Button.refresh,
            accessibilityLabel: isManualRefreshing ? "Refreshing" : "Refresh"
        ) {
            perform(.refresh)
        } label: {
            if isManualRefreshing {
                ProgressView()
                    .controlSize(.small)
                    .frame(width: 28, height: 28)
            } else {
                accessoryGlyph(systemImage: "arrow.clockwise")
            }
        }
    }

    private var healthySurface: some View {
        VStack(alignment: .leading, spacing: 0) {
            PanelSection(title: "Right now") {
                MissionReadoutGrid(readouts: primaryReadouts)

                sectionDivider

                Text(currentSupportLine)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                    .foregroundStyle(Color.secondary)
                    .monospacedDigit()
                    .lineLimit(1)
                    .minimumScaleFactor(0.78)
            }

            sectionDivider.padding(.horizontal, 4)

            managedRuntimeSurface

            if !unmanagedActivityEntries.isEmpty {
                sectionDivider.padding(.horizontal, 4)

                PanelSection(title: "Also on this Mac", trailing: snapshot.liveUnmanagedSummaryLabel) {
                    UnmanagedActivityList(entries: unmanagedActivityEntries)

                    sectionDivider

                    HStack(alignment: .center, spacing: 8) {
                        Text("Live now")
                            .font(.system(size: 10, weight: .bold, design: .monospaced))
                            .foregroundStyle(Color.secondary)
                            .tracking(0.55)

                        Spacer(minLength: 8)

                        Text(snapshot.liveUnmanagedProviderMixLabel)
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(Color.primary)
                            .lineLimit(1)
                            .minimumScaleFactor(0.82)
                            .monospacedDigit()
                    }
                }
            }
        }
    }

    private var managedRuntimeSurface: some View {
        VStack(alignment: .leading, spacing: 0) {
            if !needsUserManagedSessionEntries.isEmpty {
                PanelSection(title: "Needs you", trailing: "\(needsUserManagedSessionEntries.count)") {
                    ManagedSessionList(entries: needsUserManagedSessionEntries)
                }
            }

            if !workingManagedSessionEntries.isEmpty {
                if !needsUserManagedSessionEntries.isEmpty {
                    sectionDivider.padding(.horizontal, 4)
                }
                PanelSection(title: "Working", trailing: "\(workingManagedSessionEntries.count)") {
                    ManagedSessionList(entries: workingManagedSessionEntries)
                }
            }

            if !readyManagedSessionEntries.isEmpty {
                if !needsUserManagedSessionEntries.isEmpty || !workingManagedSessionEntries.isEmpty {
                    sectionDivider.padding(.horizontal, 4)
                }
                PanelSection(title: "Ready and background", trailing: "\(readyManagedSessionEntries.count)") {
                    ManagedSessionList(entries: readyManagedSessionEntries)
                }
            }

            if snapshot.currentManagedSessions.isEmpty {
                PanelSection(title: "Sessions") {
                    Text("No managed sessions are running on this Mac.")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Color.secondary)
                }
            }

            if !backgroundBridgeEntries.isEmpty {
                sectionDivider.padding(.horizontal, 4)

                PanelSection(title: "Cleanup needed", trailing: "\(backgroundBridgeEntries.count)") {
                    BackgroundBridgeList(
                        entries: backgroundBridgeEntries,
                        bulkStopAction: backgroundBridgeStopAllAction(),
                        bulkStopTargetCount: backgroundBridgeBulkStopTargets.count
                    )
                }
            }
        }
    }

    private var needsUserManagedSessionEntries: [ManagedSessionEntry] {
        snapshot.currentManagedSessions
            .filter { $0.explicitlyNeedsUser }
            .map { managedSessionEntry(for: $0) }
    }

    private var workingManagedSessionEntries: [ManagedSessionEntry] {
        snapshot.currentManagedSessions
            .filter { !$0.explicitlyNeedsUser && $0.menuBarAttentionKind == .working }
            .map { managedSessionEntry(for: $0) }
    }

    private var readyManagedSessionEntries: [ManagedSessionEntry] {
        snapshot.currentManagedSessions
            .filter { !$0.explicitlyNeedsUser && $0.menuBarAttentionKind != .working }
            .map { managedSessionEntry(for: $0) }
    }

    private var primaryReadouts: [PanelReadout] {
        [
            PanelReadout(
                label: "Last ship",
                value: snapshot.lastShipCompactLabel(relativeTo: presentationDate),
                detail: "Shipped",
                tone: snapshot.parsedSeverity.accentColor
            ),
            PanelReadout(
                label: "Recent",
                value: snapshot.sessionsRecentLabel,
                detail: snapshot.recentWindowCompactLabel,
                tone: snapshot.providerCountsRecent.isEmpty ? .primary : snapshot.parsedSeverity.accentColor
            ),
            PanelReadout(
                label: "Today",
                value: snapshot.sessionsTodayLabel,
                detail: "Archived"
            ),
            PanelReadout(
                label: "Queue",
                value: queueBoardValue,
                detail: queueBoardDetail,
                tone: queueBoardTone
            ),
        ]
    }

    private var currentSupportLine: String {
        [
            snapshot.launchValueLabel,
            "Heartbeat \(snapshot.engineAgeLabel(relativeTo: presentationDate))",
            "\(snapshot.diskFreeCompactLabel) free",
        ]
            .filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && $0 != "Unavailable" && $0 != "-" }
            .joined(separator: " · ")
    }

    private var headerTelemetryItems: [HeaderRailMetric] {
        [
            HeaderRailMetric(label: "Ship", value: snapshot.lastShipCompactLabel(relativeTo: presentationDate), tint: snapshot.parsedSeverity.accentColor),
            HeaderRailMetric(label: "Recent", value: snapshot.sessionsRecentLabel, tint: snapshot.providerCountsRecent.isEmpty ? .primary : snapshot.parsedSeverity.accentColor),
            HeaderRailMetric(label: "Managed", value: "\(snapshot.currentManagedSessions.count)", tint: managedChipTint),
            HeaderRailMetric(label: "Queue", value: queueBoardValue, tint: queueBoardTone),
        ]
    }

    private var headerSessionTokens: [HeaderSessionToken] {
        snapshot.currentManagedSessions.enumerated().map { index, session in
            HeaderSessionToken(
                id: "\(index)-\((session.provider ?? "unknown").lowercased())",
                provider: session.provider ?? "unknown",
                attention: session.menuBarAttentionKind
            )
        }
    }

    /// Live provider CLIs Longhouse does not own on this Mac right now.
    /// This is explicit process truth, not recent transcript activity.
    private var unmanagedActivityEntries: [UnmanagedActivityEntry] {
        snapshot.currentUnmanagedProcesses.map { process in
            let workspace = (process.workspaceLabel ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let provider = (process.provider ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return UnmanagedActivityEntry(
                id: process.id,
                provider: provider.isEmpty ? "unknown" : provider,
                title: workspace.isEmpty ? HealthSnapshot.providerDisplayName(provider.isEmpty ? "unknown" : provider) : workspace,
                branch: (process.branch ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : process.branch,
                age: snapshot.compactTimestampLabel(process.startedAt, relativeTo: presentationDate)
            )
        }
    }

    private var foregroundManagedSessionEntries: [ManagedSessionEntry] {
        snapshot.currentManagedSessions
            .filter { !$0.isConsoleManagedSession && !$0.needsManagedSessionAttention }
            .map { managedSessionEntry(for: $0) }
    }

    private var consoleManagedSessionEntries: [ManagedSessionEntry] {
        snapshot.currentManagedSessions
            .filter { $0.isConsoleManagedSession }
            .map { managedSessionEntry(for: $0) }
    }

    private var attentionManagedSessionEntries: [ManagedSessionEntry] {
        snapshot.currentManagedSessions
            .filter { $0.needsManagedSessionAttention }
            .map { managedSessionEntry(for: $0) }
    }

    private func managedSessionEntry(for session: ManagedSessionSnapshot) -> ManagedSessionEntry {
        let provider = (session.provider ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

        return ManagedSessionEntry(
            id: session.id,
            sessionID: session.sessionId,
            provider: provider.isEmpty ? "unknown" : provider,
            title: managedSessionTitle(session),
            attention: session.menuBarAttentionKind,
            ageLabel: snapshot.compactTimestampLabel(session.lastActivityAt, relativeTo: presentationDate),
            detail: managedSessionDetail(session),
            openAction: managedOpenAction(for: session),
            stopAction: managedStopAction(for: session)
        )
    }

    private var backgroundBridgeEntries: [BackgroundBridgeEntry] {
        snapshot.currentOrphanBridges.map { bridge in
            let workspace = (bridge.workspaceLabel ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let provider = (bridge.provider ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let status = (bridge.status ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

            return BackgroundBridgeEntry(
                id: bridge.id,
                sessionID: bridge.sessionId,
                provider: provider.isEmpty ? "unknown" : provider,
                workspace: workspace.isEmpty ? "Detached workspace" : workspace,
                statusLabel: status.isEmpty ? "orphan" : status,
                ageLabel: snapshot.compactTimestampLabel(bridge.heartbeatAt ?? bridge.startedAt, relativeTo: presentationDate),
                detail: orphanBridgeDetail(bridge),
                stopAction: orphanBridgeStopAction(for: bridge)
            )
        }
    }

    private var attentionManagedBulkStopTargets: [ManagedStopTarget] {
        attentionManagedSessionEntries.compactMap { entry -> ManagedStopTarget? in
            guard entry.stopAction != nil,
                  isIdleBulkStopCandidate(entry),
                  let sessionID = entry.sessionID?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !sessionID.isEmpty
            else {
                return nil
            }
            return ManagedStopTarget(sessionID: sessionID, provider: entry.provider)
        }
    }

    private var backgroundBridgeBulkStopTargets: [ManagedStopTarget] {
        backgroundBridgeEntries.compactMap { entry -> ManagedStopTarget? in
            guard entry.stopAction != nil,
                  let sessionID = entry.sessionID?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !sessionID.isEmpty
            else {
                return nil
            }
            return ManagedStopTarget(sessionID: sessionID, provider: entry.provider)
        }
    }

    private func managedStopAction(for session: ManagedSessionSnapshot) -> (() -> Void)? {
        guard session.canStopFromMenuBar,
              let sessionID = session.sessionId,
              !sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else {
            return nil
        }

        let workspace = session.workspaceLabel
        let provider = session.provider
        return {
            setFeedback(
                actionSink.handleStopManagedBridge(
                    sessionID: sessionID,
                    provider: provider,
                    workspaceLabel: workspace,
                    snapshot: snapshot
                )
            )
        }
    }

    private func managedOpenAction(for session: ManagedSessionSnapshot) -> (() -> Void)? {
        guard let sessionID = session.sessionId?.trimmingCharacters(in: .whitespacesAndNewlines),
              !sessionID.isEmpty
        else {
            return nil
        }

        let title = managedSessionTitle(session)
        return {
            setFeedback(
                actionSink.handleOpenManagedSession(
                    sessionID: sessionID,
                    title: title,
                    snapshot: snapshot
                )
            )
        }
    }

    private func orphanBridgeStopAction(for bridge: OrphanBridgeSnapshot) -> (() -> Void)? {
        guard let sessionID = bridge.sessionId,
              !sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else {
            return nil
        }

        let workspace = bridge.workspaceLabel
        let provider = bridge.provider
        return {
            setFeedback(
                actionSink.handleStopManagedBridge(
                    sessionID: sessionID,
                    provider: provider,
                    workspaceLabel: workspace,
                    snapshot: snapshot
                )
            )
        }
    }

    private func attentionManagedStopAllAction() -> (() -> Void)? {
        let targets = attentionManagedBulkStopTargets
        guard !targets.isEmpty else {
            return nil
        }

        return {
            setFeedback(
                actionSink.handleStopManagedBridges(
                    targets: targets,
                    label: "managed sessions needing attention",
                    snapshot: snapshot
                )
            )
        }
    }

    private func backgroundBridgeStopAllAction() -> (() -> Void)? {
        let targets = backgroundBridgeBulkStopTargets
        guard !targets.isEmpty else {
            return nil
        }

        return {
            setFeedback(
                actionSink.handleStopManagedBridges(
                    targets: targets,
                    label: "detached bridges",
                    snapshot: snapshot
                )
            )
        }
    }

    private func isIdleBulkStopCandidate(_ entry: ManagedSessionEntry) -> Bool {
        switch entry.attention {
        case .idle, .detached:
            return true
        case .working, .needsYou, .blocked, .degraded, .unknown:
            return false
        }
    }

    /// The secondary line keeps workspace/branch context and only adds
    /// control-path detail when it is useful. The primary line is the stable
    /// session headline; repeating workspace as the headline is too low-signal
    /// when several rows come from the same repo.
    private func managedSessionDetail(_ session: ManagedSessionSnapshot) -> String {
        let workspaceContext = managedSessionWorkspaceContext(session)
        if session.normalizedState == "attached",
           case .unknown = session.menuBarAttentionKind {
            if let rawPhase = session.rawPhase?.trimmingCharacters(in: .whitespacesAndNewlines),
               !rawPhase.isEmpty {
                return compactDetailParts([workspaceContext, "Unexpected local phase: \(rawPhase)"])
            }
            if let phase = session.phase?.trimmingCharacters(in: .whitespacesAndNewlines),
               !phase.isEmpty {
                return compactDetailParts([workspaceContext, "Unexpected local phase label: \(phase)"])
            }
            return compactDetailParts([workspaceContext, "Longhouse cannot classify this managed phase yet."])
        }

        let presenceDetail: String?
        switch session.normalizedUIPresence {
        case "foreground_tui":
            presenceDetail = "Terminal attached."
        case "background":
            presenceDetail = "Console session."
        default:
            presenceDetail = nil
        }
        if let presenceDetail {
            return compactDetailParts([workspaceContext, presenceDetail])
        }

        switch session.normalizedState {
        case "attached":
            return workspaceContext
        case "detached":
            return compactDetailParts([workspaceContext, "Window closed. Session still running in background."])
        case "degraded":
            let reasons = (session.reasonCodes ?? []).prefix(2).map { HealthSnapshot.humanizeManagedReason($0) }
            if reasons.isEmpty {
                return compactDetailParts([workspaceContext, "Control path degraded."])
            }
            return compactDetailParts([workspaceContext] + reasons)
        case "unknown":
            return compactDetailParts([workspaceContext, "Longhouse cannot classify this managed session yet."])
        default:
            let reasons = (session.reasonCodes ?? []).prefix(2).map { HealthSnapshot.humanizeManagedReason($0) }
            if !reasons.isEmpty {
                return compactDetailParts([workspaceContext] + reasons)
            }
            let normalized = session.normalizedState.trimmingCharacters(in: .whitespacesAndNewlines)
            if normalized.isEmpty {
                return workspaceContext
            }
            return compactDetailParts([workspaceContext, normalized.replacingOccurrences(of: "_", with: " ").capitalized])
        }
    }

    private func managedSessionTitle(_ session: ManagedSessionSnapshot) -> String {
        if let title = compactSessionText(session.resolvedTitleText, maxCharacters: 72) {
            return title
        }
        let provider = HealthSnapshot.providerDisplayName(session.provider ?? "Agent")
        let workspace = (session.workspaceLabel ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return workspace.isEmpty ? "\(provider) session" : "\(provider) session in \(workspace)"
    }

    private func managedSessionWorkspaceContext(_ session: ManagedSessionSnapshot) -> String {
        let workspace = (session.workspaceLabel ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let branch = (session.branch ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if workspace.isEmpty {
            return ""
        }
        if branch.isEmpty {
            return workspace
        }
        return "\(workspace) / \(branch)"
    }

    private func compactDetailParts(_ parts: [String]) -> String {
        parts
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: " · ")
    }

    private func compactSessionText(_ value: String?, maxCharacters: Int) -> String? {
        guard let value else {
            return nil
        }
        let compact = value
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .split(whereSeparator: \.isWhitespace)
            .joined(separator: " ")
        guard !compact.isEmpty else {
            return nil
        }
        if compact.count <= maxCharacters {
            return compact
        }
        return String(compact.prefix(max(1, maxCharacters - 1))).trimmingCharacters(in: .whitespacesAndNewlines) + "…"
    }

    private func orphanBridgeDetail(_ bridge: OrphanBridgeSnapshot) -> String {
        var parts = (bridge.reasonCodes ?? []).prefix(2).map { HealthSnapshot.humanizeManagedReason($0) }
        if parts.isEmpty {
            parts.append("No managed session bound")
        }

        let heartbeat = snapshot.compactTimestampLabel(bridge.heartbeatAt, relativeTo: presentationDate)
        if heartbeat != "-" {
            parts.append("heartbeat \(heartbeat)")
        }

        return parts.joined(separator: " · ")
    }

    private var queueBoardValue: String {
        let dead = Int(snapshot.spoolDeadLabel) ?? 0
        let pending = (Int(snapshot.spoolPendingLabel) ?? 0) + snapshot.storagePendingCount
        let blocked = snapshot.storageBlockedCount
        let outbox = snapshot.outboxCount

        if dead > 0 || blocked > 0 {
            return "\(dead + blocked)"
        }
        let waiting = pending + outbox
        if waiting > 0 {
            return "\(waiting)"
        }
        return "Clear"
    }

    private var queueBoardDetail: String {
        let dead = Int(snapshot.spoolDeadLabel) ?? 0
        if snapshot.storageBlockedCount > 0 {
            return "Source conflicts"
        }
        if dead > 0 {
            return "Dead letters"
        }
        let pending = (Int(snapshot.spoolPendingLabel) ?? 0) + snapshot.storagePendingCount
        if pending > 0 || snapshot.outboxCount > 0 {
            return "Waiting"
        }
        return "Transport"
    }

    private var queueBoardTone: Color {
        let dead = Int(snapshot.spoolDeadLabel) ?? 0
        let pending = (Int(snapshot.spoolPendingLabel) ?? 0) + snapshot.storagePendingCount
        if dead > 0 || snapshot.storageBlockedCount > 0 {
            return .red
        }
        if pending > 0 || snapshot.outboxCount > 0 {
            return pipelineColor
        }
        return .primary
    }

    private var blockerSection: some View {
        PanelSection(title: "Blocking Signals") {
            TelemetryTable(entries: [
                PanelTelemetryEntry(
                    label: "Service",
                    value: snapshot.serviceStatusTitle,
                    valueColor: snapshot.serviceStatusLabel == "running" ? snapshot.parsedSeverity.accentColor : .red,
                    labelIdentifier: LonghouseMenuBarAccessibilityID.Metric.service.title,
                    valueIdentifier: LonghouseMenuBarAccessibilityID.Metric.service.value
                ),
                PanelTelemetryEntry(label: "Last ship", value: snapshot.lastShipValueLabel(relativeTo: presentationDate)),
                PanelTelemetryEntry(label: "Queue", value: snapshot.pipelineValueLabel, valueColor: pipelineColor),
                PanelTelemetryEntry(
                    label: "Launch",
                    value: snapshot.launchValueLabel,
                    labelIdentifier: LonghouseMenuBarAccessibilityID.Detail.launchState.label,
                    valueIdentifier: LonghouseMenuBarAccessibilityID.Detail.launchState.value
                ),
            ])
        }
    }

    private var watchingSection: some View {
        PanelSection(title: "What’s happening") {
            Text(snapshot.attentionSummaryLabel)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Color.primary)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Section.next.tag(0))

            sectionDivider

            HStack(spacing: 8) {
                Label(snapshot.lastShipValueLabel(relativeTo: presentationDate), systemImage: "arrow.up.circle")
                Spacer(minLength: 8)
                Label(snapshot.pipelineValueLabel, systemImage: "tray")
            }
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(Color.secondary)
            .monospacedDigit()
        }
    }

    private var watchingActions: some View {
        HStack(spacing: 8) {
            Button {
                perform(.openLonghouse)
            } label: {
                Label("Open Longhouse", systemImage: "arrow.up.forward.square")
                    .frame(maxWidth: .infinity)
            }
            .modifier(SecondaryActionButtonStyle())
            .controlSize(.regular)
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.openLonghouse)

            Button {
                perform(.openLogs)
            } label: {
                Label("Logs", systemImage: "doc.text.magnifyingglass")
                    .frame(maxWidth: .infinity)
            }
            .modifier(SecondaryActionButtonStyle())
            .controlSize(.regular)
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.openLogs)
        }
    }

    private var runbookSection: some View {
        PanelSection(title: "Next") {
            Text(snapshot.attentionSummaryLabel)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Color.primary)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Section.next.tag(0))

            if !snapshot.reasons.isEmpty {
                sectionDivider

                AdaptiveTagGrid {
                    ForEach(Array(snapshot.reasons.prefix(4).enumerated()), id: \.offset) { index, reason in
                        Text(snapshotReason(reason))
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(snapshot.parsedSeverity.accentColor)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 5)
                            .background(
                                Capsule(style: .continuous)
                                    .fill(snapshot.parsedSeverity.accentColor.opacity(0.12))
                            )
                            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Section.reasons.tag(index))
                    }
                }
            }
        }
    }

    private var issueActions: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Button {
                    perform(primaryIssueAction)
                } label: {
                    Label(primaryInstallActionTitle, systemImage: primaryInstallActionSymbol)
                        .frame(maxWidth: .infinity)
                }
                .modifier(ProminentActionButtonStyle(tint: primaryInstallActionTint))
                .controlSize(.large)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.repair)
                .accessibilityLabel(Text(primaryInstallActionTitle))

                if !snapshot.isSetupRequired && !snapshot.isInstallLocationBlocked {
                    Button {
                        perform(.openLonghouse)
                    } label: {
                        Label("Open", systemImage: "arrow.up.forward.square")
                            .frame(maxWidth: .infinity)
                    }
                    .modifier(SecondaryActionButtonStyle())
                    .controlSize(.large)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.openLonghouse)
                    .accessibilityLabel(Text("Open Longhouse"))
                }
            }

            HStack(spacing: 8) {
                Button {
                    perform(.openLogs)
                } label: {
                    Label("Logs", systemImage: "doc.text.magnifyingglass")
                        .frame(maxWidth: .infinity)
                }
                .modifier(SecondaryActionButtonStyle())
                .controlSize(.regular)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.openLogs)
                .accessibilityLabel(Text("Logs"))

                Button {
                    perform(.copyDiagnostics)
                } label: {
                    Label("Copy JSON", systemImage: "doc.on.doc")
                        .frame(maxWidth: .infinity)
                }
                .modifier(SecondaryActionButtonStyle())
                .controlSize(.regular)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.copyDiagnostics)
                .accessibilityLabel(Text("Copy JSON"))
            }
        }
    }

    private var healthyToolsMenu: some View {
        Menu {
            Button("Doctor") {
                setFeedback(actionSink.handle(.runDoctor, snapshot: snapshot))
            }
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.doctor)

            Button("Logs") {
                setFeedback(actionSink.handle(.openLogs, snapshot: snapshot))
            }
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.openLogs)

            Button("Copy JSON") {
                setFeedback(actionSink.handle(.copyDiagnostics, snapshot: snapshot))
            }
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.copyDiagnostics)

            Divider()

            Button("Quit Longhouse") {
                _ = actionSink.handle(.quitApp, snapshot: snapshot)
            }
        } label: {
            accessoryGlyph(systemImage: "ellipsis")
        }
        .menuStyle(.borderlessButton)
        .controlSize(.regular)
    }

    private func headerAccessoryButton<Label: View>(
        accessibilityIdentifier: String,
        accessibilityLabel: String,
        isDisabled: Bool = false,
        action: @escaping () -> Void,
        @ViewBuilder label: () -> Label
    ) -> some View {
        Button(action: action) {
            label()
        }
        .buttonStyle(.plain)
        .disabled(isDisabled)
        .accessibilityIdentifier(accessibilityIdentifier)
        .accessibilityLabel(Text(accessibilityLabel))
    }

    private func headerAccessoryButton(
        systemImage: String,
        accessibilityIdentifier: String,
        accessibilityLabel: String,
        isDisabled: Bool = false,
        action: @escaping () -> Void
    ) -> some View {
        headerAccessoryButton(
            accessibilityIdentifier: accessibilityIdentifier,
            accessibilityLabel: accessibilityLabel,
            isDisabled: isDisabled,
            action: action
        ) {
            accessoryGlyph(systemImage: systemImage)
        }
    }

    private func accessoryGlyph(systemImage: String) -> some View {
        Image(systemName: systemImage)
            .font(.system(size: 13, weight: .medium))
            .foregroundStyle(Color.secondary)
            .frame(width: 26, height: 26)
            .contentShape(Rectangle())
    }

    private var pipelineColor: Color {
        if snapshot.spoolDeadLabel != "0" {
            return .red
        }
        if snapshot.spoolPendingLabel != "0" || snapshot.outboxCount > 0 {
            return .orange
        }
        return snapshot.parsedSeverity == .green ? Color.primary : snapshot.parsedSeverity.accentColor
    }

    private var managedChipTint: Color {
        if let severity = snapshot.managedAttentionSeverity {
            return severity.accentColor
        }
        return Color.secondary
    }

    private var primaryInstallActionTitle: String {
        if snapshot.isInstallLocationBlocked {
            return "Quit"
        }
        return snapshot.isSetupRequired ? "Set Up" : "Repair"
    }

    private var primaryInstallActionSymbol: String {
        if snapshot.isInstallLocationBlocked {
            return "xmark.circle"
        }
        return snapshot.isSetupRequired ? "square.and.arrow.down" : "wrench.and.screwdriver"
    }

    private var primaryInstallActionTint: Color {
        if snapshot.isInstallLocationBlocked {
            return .gray
        }
        return snapshot.isSetupRequired ? .blue : .red
    }

    private var primaryIssueAction: HarnessAction {
        snapshot.isInstallLocationBlocked ? .quitApp : .repairInstall
    }

    private func perform(_ action: HarnessAction) {
        setFeedback(actionSink.handle(action, snapshot: snapshot))
        if action == .refresh {
            refresh()
        }
    }

    private func feedbackBanner(_ feedback: HealthActionFeedback) -> some View {
        let tint = feedbackColor(for: feedback.style)

        return HStack(alignment: .top, spacing: 10) {
            Image(systemName: feedbackIcon(for: feedback.style))
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(tint)
                .frame(width: 16, height: 16)
                .padding(.top, 1)

            VStack(alignment: .leading, spacing: 3) {
                Text(feedback.title)
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Color.white.opacity(0.96))
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.title)

                Text(feedback.detail)
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(Color.white.opacity(0.8))
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.detail)
            }

            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(feedbackBackgroundColor(for: feedback.style))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(tint.opacity(0.5), lineWidth: 1)
        )
        .shadow(color: Color.black.opacity(0.22), radius: 12, x: 0, y: 8)
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.container)
    }

    private func feedbackColor(for style: HealthActionFeedbackStyle) -> Color {
        switch style {
        case .info:
            return .blue
        case .success:
            return .green
        case .warning:
            return .orange
        case .failure:
            return .red
        }
    }

    private func feedbackIcon(for style: HealthActionFeedbackStyle) -> String {
        switch style {
        case .info:
            return "info.circle.fill"
        case .success:
            return "checkmark.circle.fill"
        case .warning:
            return "exclamationmark.triangle.fill"
        case .failure:
            return "xmark.circle.fill"
        }
    }

    private func feedbackBackgroundColor(for style: HealthActionFeedbackStyle) -> Color {
        switch style {
        case .info:
            return Color(red: 0.13, green: 0.19, blue: 0.28)
        case .success:
            return Color(red: 0.12, green: 0.24, blue: 0.18)
        case .warning:
            return Color(red: 0.29, green: 0.20, blue: 0.11)
        case .failure:
            return Color(red: 0.30, green: 0.14, blue: 0.14)
        }
    }

    private func snapshotReason(_ raw: String) -> String {
        raw
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
            .capitalized
    }
}
