import AppKit
import SwiftUI

public enum MenuBarPanelLayout {
    public static let panelWidth: CGFloat = 372
    public static let loadingHeight: CGFloat = 170
    public static let failureHeight: CGFloat = 198
    public static let healthyHeight: CGFloat = 452
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
                    healthyTelemetryDeck
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

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            statusEmblem(color: snapshot.parsedSeverity.accentColor, systemImage: snapshot.parsedSeverity.symbolName)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Header.statusGlyph)

            VStack(alignment: .leading, spacing: 7) {
                Text("LONGHOUSE LOCAL")
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundStyle(Color.secondary)
                    .tracking(0.9)

                Text(displayHeadline)
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundStyle(Color.primary)
                    .lineLimit(2)
                    .minimumScaleFactor(0.8)
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

                    subtleChip(title: "Snapshot \(snapshot.snapshotAgeCompactLabel)")

                    if let updateBadge = snapshot.updateBadgeLabel {
                        subtleChip(title: updateBadge, tint: .blue)
                    }
                }
            }

            Spacer(minLength: 0)

            HStack(spacing: 8) {
                if isHealthy {
                    Button {
                        perform(.openLonghouse)
                    } label: {
                        Image(systemName: "arrow.up.forward.square")
                            .font(.system(size: 15, weight: .semibold))
                            .frame(width: 28, height: 28)
                            .background(
                                RoundedRectangle(cornerRadius: 10, style: .continuous)
                                    .fill(Color.white.opacity(0.05))
                            )
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.openLonghouse)
                    .accessibilityLabel(Text("Open Longhouse"))

                    healthyToolsMenu
                }

                refreshControl
            }
        }
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

    private var refreshControl: some View {
        Button {
            perform(.refresh)
        } label: {
            ZStack {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(Color.white.opacity(0.05))

                if isRefreshing {
                    ProgressView()
                        .controlSize(.small)
                } else {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Color.primary)
                }
            }
            .frame(width: 28, height: 28)
        }
        .buttonStyle(.plain)
        .disabled(isRefreshing)
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.refresh)
        .accessibilityLabel(Text(isRefreshing ? "Refreshing" : "Refresh"))
    }

    private var healthyTelemetryDeck: some View {
        TelemetryDeck {
            HStack(alignment: .top, spacing: 14) {
                healthyNowColumn

                Rectangle()
                    .fill(Color.white.opacity(0.06))
                    .frame(width: 1)

                healthyTodayColumn
            }

            sectionDivider

            VStack(alignment: .leading, spacing: 9) {
                HStack(alignment: .center, spacing: 8) {
                    deckColumnTitle("Recent Pulse")

                    Spacer(minLength: 8)

                    Text(pulseTrailingLabel)
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Color.secondary)
                        .monospacedDigit()
                }

                if history.count > 1 {
                    PulseChart(history: history)
                } else {
                    Text("Collecting live shipping samples")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(Color.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.vertical, 10)
                }
            }
        }
    }

    private var healthyNowColumn: some View {
        VStack(alignment: .leading, spacing: 10) {
            deckColumnTitle("Now")

            TelemetryRow(
                label: "Last ship",
                value: snapshot.lastShipValueLabel,
                valueIdentifier: LonghouseMenuBarAccessibilityID.Header.lastShip
            )

            sectionDivider

            TelemetryRow(label: "Engine", value: snapshot.engineFreshnessValueLabel)

            sectionDivider

            TelemetryRow(
                label: "Launch",
                value: snapshot.launchValueLabel,
                labelIdentifier: LonghouseMenuBarAccessibilityID.Detail.launchState.label,
                valueIdentifier: LonghouseMenuBarAccessibilityID.Detail.launchState.value
            )

            sectionDivider

            TelemetryRow(
                label: "Queue",
                value: snapshot.pipelineValueLabel,
                valueColor: pipelineColor
            )
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }

    private var healthyTodayColumn: some View {
        VStack(alignment: .leading, spacing: 10) {
            deckColumnTitle("Today")

            HStack(alignment: .top, spacing: 10) {
                MissionStat(
                    label: "Synced",
                    value: snapshot.sessionsTodayLabel,
                    detail: "Today"
                )

                MissionStat(
                    label: "Active",
                    value: snapshot.sessionsRecentLabel,
                    detail: snapshot.recentWindowLabel
                )
            }

            sectionDivider

            TelemetryRow(label: "Last activity", value: snapshot.latestActivityLabel)

            sectionDivider

            VStack(alignment: .leading, spacing: 8) {
                Text("Provider mix")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Color.secondary)

                Text(snapshot.providerMixLabel)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Color.primary)
                    .lineLimit(2)
                    .minimumScaleFactor(0.84)
                    .fixedSize(horizontal: false, vertical: true)

                ProviderMixBar(entries: snapshot.providerCountsToday)
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
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
            TelemetryRow(
                label: "Service",
                value: snapshot.serviceStatusTitle,
                valueColor: snapshot.serviceStatusLabel == "running" ? snapshot.parsedSeverity.accentColor : .red,
                labelIdentifier: LonghouseMenuBarAccessibilityID.Metric.service.title,
                valueIdentifier: LonghouseMenuBarAccessibilityID.Metric.service.value
            )

            sectionDivider

            TelemetryRow(label: "Last ship", value: snapshot.lastShipValueLabel)

            sectionDivider

            TelemetryRow(
                label: "Queue",
                value: snapshot.pipelineValueLabel,
                valueColor: pipelineColor
            )

            sectionDivider

            TelemetryRow(
                label: "Launch",
                value: snapshot.launchValueLabel,
                labelIdentifier: LonghouseMenuBarAccessibilityID.Detail.launchState.label,
                valueIdentifier: LonghouseMenuBarAccessibilityID.Detail.launchState.value
            )
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

                FlowLayout(spacing: 6, rowSpacing: 6) {
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
            Image(systemName: "ellipsis.circle")
                .font(.system(size: 16, weight: .semibold))
                .frame(width: 28, height: 28)
                .background(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(Color.white.opacity(0.05))
                )
        }
        .menuStyle(.borderlessButton)
        .controlSize(.regular)
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

private let panelCornerRadius: CGFloat = 20
private let sectionCornerRadius: CGFloat = 14

private struct PanelChrome<Content: View>: View {
    let height: CGFloat
    let accent: Color
    let content: Content

    init(height: CGFloat, accent: Color, @ViewBuilder content: () -> Content) {
        self.height = height
        self.accent = accent
        self.content = content()
    }

    var body: some View {
        ZStack(alignment: .topLeading) {
            PanelMaterialBackground()

            LinearGradient(
                colors: [accent.opacity(0.07), Color.clear],
                startPoint: .top,
                endPoint: .bottom
            )

            content
                .padding(16)
        }
        .frame(width: MenuBarPanelLayout.panelWidth, height: height, alignment: .topLeading)
        .clipShape(RoundedRectangle(cornerRadius: panelCornerRadius, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: panelCornerRadius, style: .continuous)
                .strokeBorder(Color.white.opacity(0.1), lineWidth: 1)
        )
        .overlay(alignment: .top) {
            Capsule(style: .continuous)
                .fill(accent.opacity(0.7))
                .frame(height: 3)
                .padding(.horizontal, 16)
                .padding(.top, 12)
        }
        .shadow(color: Color.black.opacity(0.22), radius: 14, x: 0, y: 8)
    }
}

private struct PanelMaterialBackground: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .hudWindow
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {}
}

private struct PanelSection<Content: View>: View {
    let title: String
    let trailing: String?
    let content: Content

    init(title: String, trailing: String? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.trailing = trailing
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center, spacing: 8) {
                Text(title.uppercased())
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Color.secondary)
                    .tracking(0.6)

                Spacer(minLength: 8)

                if let trailing {
                    Text(trailing)
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Color.secondary)
                        .monospacedDigit()
                }
            }

            content
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: sectionCornerRadius, style: .continuous)
                .fill(Color.black.opacity(0.15))
        )
        .overlay(
            RoundedRectangle(cornerRadius: sectionCornerRadius, style: .continuous)
                .stroke(Color.white.opacity(0.08), lineWidth: 1)
        )
    }
}

private struct TelemetryDeck<Content: View>: View {
    let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            content
        }
        .padding(14)
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(Color.black.opacity(0.15))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(Color.white.opacity(0.08), lineWidth: 1)
        )
    }
}

private struct TelemetryRow: View {
    let label: String
    let value: String
    var valueColor: Color = .primary
    var labelIdentifier: String? = nil
    var valueIdentifier: String? = nil

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(label)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Color.secondary)
                .applyHarnessAccessibility(identifier: labelIdentifier, label: label)

            Spacer(minLength: 12)

            Text(value)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(valueColor)
                .monospacedDigit()
                .multilineTextAlignment(.trailing)
                .lineLimit(2)
                .minimumScaleFactor(0.8)
                .applyHarnessAccessibility(identifier: valueIdentifier, label: value)
        }
    }
}

private struct MissionStat: View {
    let label: String
    let value: String
    let detail: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label.uppercased())
                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                .foregroundStyle(Color.secondary)
                .tracking(0.5)

            Text(value)
                .font(.system(size: 23, weight: .bold))
                .foregroundStyle(Color.primary)
                .monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.75)

            Text(detail.uppercased())
                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                .foregroundStyle(Color.secondary)
                .tracking(0.45)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ProviderMixBar: View {
    let entries: [(provider: String, count: Int)]

    var body: some View {
        ZStack(alignment: .leading) {
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .fill(Color.white.opacity(0.05))

            GeometryReader { geometry in
                let total = max(entries.map(\.count).reduce(0, +), 1)

                HStack(spacing: 4) {
                    ForEach(Array(entries.enumerated()), id: \.offset) { _, entry in
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .fill(providerColor(entry.provider))
                            .frame(width: max(24, geometry.size.width * CGFloat(entry.count) / CGFloat(total)))
                            .overlay(alignment: .center) {
                                if geometry.size.width > 180 {
                                    Text(providerAbbreviation(entry.provider))
                                        .font(.system(size: 10, weight: .bold))
                                        .foregroundStyle(Color.white.opacity(0.9))
                                }
                            }
                    }
                }
                .padding(1)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(height: 16)
    }

    private func providerAbbreviation(_ raw: String) -> String {
        switch raw.lowercased() {
        case "claude":
            return "C"
        case "codex":
            return "X"
        case "gemini":
            return "G"
        default:
            return String(raw.prefix(1)).uppercased()
        }
    }

    private func providerColor(_ raw: String) -> Color {
        switch raw.lowercased() {
        case "claude":
            return Color(red: 0.39, green: 0.72, blue: 0.56)
        case "codex":
            return Color(red: 0.33, green: 0.57, blue: 0.88)
        case "gemini":
            return Color(red: 0.82, green: 0.64, blue: 0.26)
        default:
            return Color.secondary
        }
    }
}

private struct PulseChart: View {
    let history: [SnapshotHistorySample]

    var body: some View {
        GeometryReader { geometry in
            let samples = reducedSamples(from: history, maxPoints: 24)
            let maxValue = max(samples.map(activityValue(for:)).max() ?? 0, 1)
            let barWidth = max(4, (geometry.size.width - CGFloat(max(samples.count - 1, 0)) * 3) / CGFloat(max(samples.count, 1)))

            ZStack(alignment: .bottomLeading) {
                VStack(spacing: geometry.size.height / 3) {
                    Rectangle().fill(Color.white.opacity(0.04)).frame(height: 1)
                    Rectangle().fill(Color.white.opacity(0.04)).frame(height: 1)
                    Rectangle().fill(Color.white.opacity(0.04)).frame(height: 1)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)

                HStack(alignment: .bottom, spacing: 3) {
                    ForEach(Array(samples.enumerated()), id: \.offset) { _, sample in
                        let value = max(activityValue(for: sample), 0)
                        let normalized = CGFloat(value) / CGFloat(maxValue)
                        Capsule(style: .continuous)
                            .fill(color(for: sample))
                            .frame(width: barWidth, height: max(6, normalized * geometry.size.height))
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomLeading)
            }
        }
        .frame(height: 36)
    }

    private func reducedSamples(from samples: [SnapshotHistorySample], maxPoints: Int) -> [SnapshotHistorySample] {
        guard samples.count > maxPoints else {
            return samples
        }

        let stride = Double(samples.count) / Double(maxPoints)
        return (0..<maxPoints).compactMap { index in
            let sourceIndex = Int((Double(index) * stride).rounded(.down))
            guard sourceIndex < samples.count else {
                return nil
            }
            return samples[sourceIndex]
        }
    }

    private func activityValue(for sample: SnapshotHistorySample) -> Int {
        max(sample.sessionsRecent, sample.spoolPendingCount + sample.outboxCount)
    }

    private func color(for sample: SnapshotHistorySample) -> Color {
        switch sample.severity {
        case .green:
            return Color(red: 0.29, green: 0.77, blue: 0.47)
        case .yellow:
            return Color(red: 0.92, green: 0.74, blue: 0.28)
        case .red:
            return Color(red: 0.90, green: 0.34, blue: 0.28)
        case .gray:
            return Color.secondary
        }
    }
}

private struct FlowLayout<Content: View>: View {
    let spacing: CGFloat
    let rowSpacing: CGFloat
    let content: Content

    init(spacing: CGFloat, rowSpacing: CGFloat, @ViewBuilder content: () -> Content) {
        self.spacing = spacing
        self.rowSpacing = rowSpacing
        self.content = content()
    }

    var body: some View {
        content
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

@MainActor
private func statusChip(title: String, color: Color, identifier: String? = nil) -> some View {
    Text(title)
        .font(.system(size: 10, weight: .bold, design: .monospaced))
        .foregroundStyle(color)
        .padding(.horizontal, 9)
        .padding(.vertical, 5)
        .background(
            Capsule(style: .continuous)
                .fill(color.opacity(0.14))
        )
        .applyHarnessAccessibility(identifier: identifier, label: title)
}

private func subtleChip(title: String, tint: Color = Color.secondary) -> some View {
    Text(title)
        .font(.system(size: 10, weight: .semibold, design: .monospaced))
        .foregroundStyle(tint)
        .padding(.horizontal, 9)
        .padding(.vertical, 5)
        .background(
            Capsule(style: .continuous)
                .fill(Color.white.opacity(0.05))
        )
}

private var sectionDivider: some View {
    Rectangle()
        .fill(Color.white.opacity(0.06))
        .frame(height: 1)
}

private func deckColumnTitle(_ title: String) -> some View {
    Text(title.uppercased())
        .font(.system(size: 10, weight: .bold, design: .monospaced))
        .foregroundStyle(Color.secondary)
        .tracking(0.7)
}

private func statusEmblem(color: Color, systemImage: String) -> some View {
    ZStack {
        Circle()
            .fill(color.opacity(0.14))
            .frame(width: 34, height: 34)
        Image(systemName: systemImage)
            .font(.system(size: 16, weight: .semibold))
            .foregroundStyle(color)
    }
}

private extension View {
    func harnessAccessibility(identifier: String, label: String) -> some View {
        accessibilityIdentifier(identifier)
            .accessibilityLabel(Text(label))
    }

    @ViewBuilder
    func applyHarnessAccessibility(identifier: String?, label: String) -> some View {
        if let identifier {
            accessibilityIdentifier(identifier)
                .accessibilityLabel(Text(label))
        } else {
            self
        }
    }
}
