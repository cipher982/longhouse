import SwiftUI

public enum MenuBarPanelLayout {
    public static let panelWidth: CGFloat = 376
    public static let loadingHeight: CGFloat = 170
    public static let failureHeight: CGFloat = 198
    public static let healthyHeight: CGFloat = 560
    public static let attentionHeight: CGFloat = 564

    public static func preferredHeight(for snapshot: HealthSnapshot) -> CGFloat {
        snapshot.parsedSeverity == .green ? healthyHeight : attentionHeight
    }
}

public struct MenuBarLoadingView: View {
    public init() {}

    public var body: some View {
        PanelChrome(height: MenuBarPanelLayout.loadingHeight, accent: .gray) {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .center, spacing: 12) {
                    statusEmblem(color: .gray, systemImage: "arrow.trianglehead.clockwise")

                    VStack(alignment: .leading, spacing: 3) {
                        Text("Refreshing local shipping")
                            .font(.system(size: 18, weight: .semibold))
                            .foregroundStyle(Color.primary)
                        Text("Longhouse is collecting the latest machine snapshot.")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(Color.secondary)
                    }
                }

                PanelSection(title: "Snapshot") {
                    HStack(spacing: 10) {
                        ProgressView()
                            .controlSize(.small)
                        Text("Loading cached health and telemetry")
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
        PanelChrome(height: MenuBarPanelLayout.failureHeight, accent: .red) {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .center, spacing: 12) {
                    statusEmblem(color: .red, systemImage: "xmark.circle.fill")
                        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Error.headline)
                        .accessibilityLabel(Text("Longhouse could not load local health"))

                    VStack(alignment: .leading, spacing: 3) {
                        Text("Health snapshot unavailable")
                            .font(.system(size: 18, weight: .semibold))
                            .foregroundStyle(Color.primary)
                        Text("The local menu bar surface could not load its latest snapshot.")
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
    private let actionSink: any HealthActionSink
    private let isRefreshing: Bool
    private let refresh: () -> Void

    @State private var feedback: HealthActionFeedback?

    public init(
        snapshot: HealthSnapshot,
        history: [SnapshotHistorySample],
        actionSink: any HealthActionSink,
        isRefreshing: Bool,
        refresh: @escaping () -> Void
    ) {
        self.snapshot = snapshot
        self.history = history
        self.actionSink = actionSink
        self.isRefreshing = isRefreshing
        self.refresh = refresh
        _feedback = State(initialValue: nil)
    }

    public var body: some View {
        PanelChrome(height: MenuBarPanelLayout.preferredHeight(for: snapshot), accent: snapshot.parsedSeverity.accentColor) {
            VStack(alignment: .leading, spacing: 14) {
                header

                if isHealthy {
                    healthySurface
                } else {
                    blockerSection
                    runbookSection
                    issueActions
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .overlay(alignment: .bottomLeading) {
            if let feedback {
                feedbackBanner(feedback)
                    .padding(.horizontal, 16)
                    .padding(.bottom, 16)
                    .allowsHitTesting(false)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.panel)
    }

    private var isHealthy: Bool {
        snapshot.parsedSeverity == .green && snapshot.healthState.lowercased() == "healthy"
    }

    private var displayHeadline: String {
        let trimmed = snapshot.headline.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.lowercased().hasPrefix("longhouse ") else {
            return trimmed
        }

        let dropped = String(trimmed.dropFirst("Longhouse ".count))
        guard let first = dropped.first else {
            return trimmed
        }
        return first.uppercased() + dropped.dropFirst()
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            statusEmblem(color: snapshot.parsedSeverity.accentColor, systemImage: snapshot.parsedSeverity.symbolName)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Header.statusGlyph)

            VStack(alignment: .leading, spacing: 8) {
                Text("LONGHOUSE LOCAL")
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

                HStack(spacing: 8) {
                    statusChip(
                        title: snapshot.ambientStatusLabel.uppercased(),
                        color: snapshot.parsedSeverity.accentColor,
                        identifier: LonghouseMenuBarAccessibilityID.Header.statusBadge
                    )

                    subtleChip(title: "Updated \(snapshot.snapshotAgeCompactLabel)")

                    if let updateBadge = snapshot.updateBadgeLabel {
                        subtleChip(title: updateBadge, tint: .blue)
                    }
                }

                Text(snapshot.missionSummaryLabel)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Color.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.84)
            }

            Spacer(minLength: 0)

            headerControlGroup
        }
    }

    private var headerControlGroup: some View {
        HStack(spacing: 6) {
            if isHealthy {
                headerAccessoryButton(
                    systemImage: "arrow.up.forward.square",
                    accessibilityIdentifier: LonghouseMenuBarAccessibilityID.Button.openLonghouse,
                    accessibilityLabel: "Open Longhouse"
                ) {
                    perform(.openLonghouse)
                }

                healthyToolsMenu
            }

            refreshControl
        }
    }

    private var refreshControl: some View {
        headerAccessoryButton(
            accessibilityIdentifier: LonghouseMenuBarAccessibilityID.Button.refresh,
            accessibilityLabel: isRefreshing ? "Refreshing" : "Refresh",
            isDisabled: isRefreshing
        ) {
            perform(.refresh)
        } label: {
            if isRefreshing {
                ProgressView()
                    .controlSize(.small)
                    .frame(width: 28, height: 28)
            } else {
                accessoryGlyph(systemImage: "arrow.clockwise")
            }
        }
    }

    private var healthySurface: some View {
        VStack(alignment: .leading, spacing: 12) {
            MissionReadoutGrid(readouts: healthyReadouts)

            PanelSection(title: "Operations") {
                TelemetryTable(entries: healthyOperationsEntries)

                if !snapshot.providerCountsToday.isEmpty {
                    sectionDivider

                    VStack(alignment: .leading, spacing: 6) {
                        Text("PROVIDER MIX")
                            .font(.system(size: 9, weight: .bold, design: .monospaced))
                            .foregroundStyle(Color.secondary)
                            .tracking(0.55)

                        Text(snapshot.providerMixLabel)
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(Color.primary)
                            .lineLimit(1)
                            .minimumScaleFactor(0.76)
                            .monospacedDigit()

                        ProviderMixBar(entries: snapshot.providerCountsToday)
                    }
                }
            }

            PanelSection(title: "Recent Pulse", trailing: pulseTrailingLabel) {
                if history.count > 1 {
                    PulseChart(history: history)

                    HStack {
                        Text("Last \(SnapshotStore.historyRetentionMinutes)m")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(Color.secondary)
                        Spacer()
                        Text("Now")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(Color.secondary)
                    }
                } else {
                    Text("Collecting live shipping samples")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(Color.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.vertical, 8)
                }
            }
        }
    }

    private var healthyReadouts: [PanelReadout] {
        [
            PanelReadout(label: "Ship", value: snapshot.lastShipCompactLabel, detail: "Last ship"),
            PanelReadout(label: "Beat", value: snapshot.engineAgeLabel, detail: snapshot.engineFreshnessLabel),
            PanelReadout(label: "Today", value: snapshot.sessionsTodayLabel, detail: "Sessions"),
            PanelReadout(label: snapshot.recentWindowCompactLabel, value: snapshot.sessionsRecentLabel, detail: "Active"),
        ]
    }

    private var healthyOperationsEntries: [PanelTelemetryEntry] {
        var entries = [
            PanelTelemetryEntry(
                label: "Launch",
                value: snapshot.launchValueLabel,
                labelIdentifier: LonghouseMenuBarAccessibilityID.Detail.launchState.label,
                valueIdentifier: LonghouseMenuBarAccessibilityID.Detail.launchState.value
            ),
            PanelTelemetryEntry(label: "Heartbeat", value: snapshot.engineFreshnessValueLabel),
            PanelTelemetryEntry(
                label: "Buffers",
                value: "OUT \(snapshot.outboxCount) · Q \(snapshot.spoolPendingLabel) · DEAD \(snapshot.spoolDeadLabel)",
                valueColor: pipelineColor
            ),
            PanelTelemetryEntry(label: "Latest", value: snapshot.latestActivityLabel),
        ]

        if let updateBadge = snapshot.updateBadgeLabel {
            entries.append(PanelTelemetryEntry(label: "Update", value: updateBadge, valueColor: .blue))
        }

        return entries
    }

    private var pulseTrailingLabel: String {
        if let latest = history.last {
            if latest.sessionsRecent > 0 {
                return "\(latest.sessionsRecent) active"
            }
            if latest.spoolPendingCount > 0 || latest.outboxCount > 0 {
                return "Queue busy"
            }
        }
        return "Idle"
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
                PanelTelemetryEntry(label: "Last ship", value: snapshot.lastShipValueLabel),
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
                    perform(.repairInstall)
                } label: {
                    Label("Repair", systemImage: "wrench.and.screwdriver")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(.red)
                .controlSize(.large)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.repair)
                .accessibilityLabel(Text("Repair"))

                Button {
                    perform(.openLonghouse)
                } label: {
                    Label("Open", systemImage: "arrow.up.forward.square")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .controlSize(.large)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.openLonghouse)
                .accessibilityLabel(Text("Open Longhouse"))
            }

            HStack(spacing: 8) {
                Button {
                    perform(.openLogs)
                } label: {
                    Label("Logs", systemImage: "doc.text.magnifyingglass")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .controlSize(.regular)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.openLogs)
                .accessibilityLabel(Text("Logs"))

                Button {
                    perform(.copyDiagnostics)
                } label: {
                    Label("Copy JSON", systemImage: "doc.on.doc")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .controlSize(.regular)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.copyDiagnostics)
                .accessibilityLabel(Text("Copy JSON"))
            }
        }
    }

    private var healthyToolsMenu: some View {
        Menu {
            if snapshot.updateInfo?.updateAvailable == true {
                Button("Upgrade") {
                    feedback = actionSink.handle(.upgradeNow, snapshot: snapshot)
                }
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.upgradeNow)
            }

            Button("Doctor") {
                feedback = actionSink.handle(.runDoctor, snapshot: snapshot)
            }
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.doctor)

            Button("Logs") {
                feedback = actionSink.handle(.openLogs, snapshot: snapshot)
            }
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.openLogs)

            Button("Copy JSON") {
                feedback = actionSink.handle(.copyDiagnostics, snapshot: snapshot)
            }
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.copyDiagnostics)
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
            .font(.system(size: 14, weight: .semibold))
            .foregroundStyle(Color.primary)
            .frame(width: 28, height: 28)
            .background(
                RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .fill(Color.white.opacity(0.05))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .stroke(Color.white.opacity(0.06), lineWidth: 1)
            )
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

    private func perform(_ action: HarnessAction) {
        feedback = actionSink.handle(action, snapshot: snapshot)
        if action == .refresh {
            refresh()
        }
    }

    private func feedbackBanner(_ feedback: HealthActionFeedback) -> some View {
        let tint = feedbackColor(for: feedback.style)

        return HStack(alignment: .center, spacing: 8) {
            Image(systemName: feedbackIcon(for: feedback.style))
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(tint)

            VStack(alignment: .leading, spacing: 1) {
                Text(feedback.title)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Color.primary)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.title)

                Text(feedback.detail)
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(Color.secondary)
                    .lineLimit(2)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.detail)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color.black.opacity(0.28))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(tint.opacity(0.24), lineWidth: 1)
        )
        .shadow(color: Color.black.opacity(0.24), radius: 10, x: 0, y: 6)
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

    private func snapshotReason(_ raw: String) -> String {
        raw
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
            .capitalized
    }
}
