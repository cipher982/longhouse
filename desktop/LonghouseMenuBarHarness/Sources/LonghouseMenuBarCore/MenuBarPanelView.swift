import AppKit
import SwiftUI

public enum MenuBarPanelLayout {
    public static let panelWidth: CGFloat = 336
    public static let loadingHeight: CGFloat = 154
    public static let failureHeight: CGFloat = 190
    public static let healthyHeight: CGFloat = 196
    public static let attentionHeight: CGFloat = 368

    public static func preferredHeight(for snapshot: HealthSnapshot) -> CGFloat {
        var height = snapshot.parsedSeverity == .green ? healthyHeight : attentionHeight
        if snapshot.updateInfo?.updateAvailable == true {
            height += 44
        }
        return height
    }
}

public struct MenuBarLoadingView: View {
    public init() {}

    public var body: some View {
        PanelChrome(height: MenuBarPanelLayout.loadingHeight) {
            VStack(alignment: .leading, spacing: 12) {
                statusEmblem(color: .gray, systemImage: "arrow.trianglehead.clockwise")

                Text("Checking local shipping")
                    .font(.system(size: 16, weight: .semibold, design: .rounded))
                    .foregroundStyle(Color.primary)

                Text("Longhouse is refreshing the latest machine health snapshot.")
                    .font(.system(size: 12, weight: .medium, design: .rounded))
                    .foregroundStyle(Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                HStack(spacing: 8) {
                    ProgressView()
                        .controlSize(.small)
                    Text("Loading")
                        .font(.system(size: 12, weight: .semibold, design: .rounded))
                        .foregroundStyle(Color.secondary)
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
        PanelChrome(height: MenuBarPanelLayout.failureHeight) {
            VStack(alignment: .leading, spacing: 12) {
                statusEmblem(color: .red, systemImage: "xmark.circle.fill")
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Error.headline)
                    .accessibilityLabel(Text("Longhouse could not load local health"))

                Text("Longhouse could not load local health")
                    .font(.system(size: 16, weight: .semibold, design: .rounded))
                    .foregroundStyle(Color.primary)

                Text(message)
                    .font(.system(size: 12, weight: .medium, design: .rounded))
                    .foregroundStyle(Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Error.message)

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
    private let actionSink: any HealthActionSink
    private let isRefreshing: Bool
    private let refresh: () -> Void

    @State private var feedback: HealthActionFeedback?

    public init(
        snapshot: HealthSnapshot,
        actionSink: any HealthActionSink,
        isRefreshing: Bool,
        refresh: @escaping () -> Void
    ) {
        self.snapshot = snapshot
        self.actionSink = actionSink
        self.isRefreshing = isRefreshing
        self.refresh = refresh
        _feedback = State(initialValue: nil)
    }

    public var body: some View {
        PanelChrome(height: MenuBarPanelLayout.preferredHeight(for: snapshot)) {
            VStack(alignment: .leading, spacing: 12) {
                header

                if snapshot.updateInfo?.updateAvailable == true {
                    updateBanner
                }

                if isHealthy {
                    healthyDetails
                    healthyActions
                } else {
                    issueDetails
                    attentionCallout
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

            VStack(alignment: .leading, spacing: 4) {
                Text(snapshot.headline)
                    .font(.system(size: 16, weight: .semibold, design: .rounded))
                    .foregroundStyle(Color.primary)
                    .fixedSize(horizontal: false, vertical: true)
                    .harnessAccessibility(
                        identifier: LonghouseMenuBarAccessibilityID.Header.headline,
                        label: snapshot.headline
                    )

                Text(snapshot.ambientStatusLabel)
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .foregroundStyle(snapshot.parsedSeverity.accentColor)
                    .harnessAccessibility(
                        identifier: LonghouseMenuBarAccessibilityID.Header.statusBadge,
                        label: snapshot.ambientStatusLabel
                    )
            }

            Spacer(minLength: 0)

            refreshControl
        }
    }

    private var refreshControl: some View {
        Button {
            perform(.refresh)
        } label: {
            ZStack {
                if isRefreshing {
                    ProgressView()
                        .controlSize(.small)
                } else {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 13, weight: .semibold))
                }
            }
            .frame(width: 18, height: 18)
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .disabled(isRefreshing)
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.refresh)
        .accessibilityLabel(Text(isRefreshing ? "Refreshing" : "Refresh"))
    }

    private var updateBanner: some View {
        let latest = snapshot.updateInfo?.latestVersion?.trimmingCharacters(in: .whitespacesAndNewlines)
        let installed = snapshot.updateInfo?.installedVersion ?? ""
        let label = if let latest, !latest.isEmpty {
            "Version \(latest) is ready. Installed: \(installed)."
        } else {
            "A Longhouse update is ready."
        }

        return HStack(alignment: .center, spacing: 10) {
            Image(systemName: "arrow.down.circle.fill")
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(Color.blue)

            Text(label)
                .font(.system(size: 11, weight: .semibold, design: .rounded))
                .foregroundStyle(Color.primary)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.UpdateBanner.label)

            Spacer(minLength: 0)

            Button {
                feedback = actionSink.handle(.upgradeNow, snapshot: snapshot)
            } label: {
                Text("Upgrade")
            }
            .buttonStyle(.borderedProminent)
            .tint(.blue)
            .controlSize(.small)
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.upgradeNow)
            .accessibilityLabel(Text("Upgrade"))
        }
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: sectionCornerRadius, style: .continuous)
                .fill(Color.blue.opacity(0.08))
        )
        .overlay(
            RoundedRectangle(cornerRadius: sectionCornerRadius, style: .continuous)
                .stroke(Color.blue.opacity(0.16), lineWidth: 1)
        )
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.UpdateBanner.container)
    }

    private var healthyDetails: some View {
        PanelSection {
            PanelValueRow(
                label: "Last ship",
                value: snapshot.lastShipValueLabel,
                valueIdentifier: LonghouseMenuBarAccessibilityID.Header.lastShip
            )

            Divider()

            PanelValueRow(
                label: "Launch",
                value: snapshot.launchValueLabel,
                labelIdentifier: LonghouseMenuBarAccessibilityID.Detail.launchState.label,
                valueIdentifier: LonghouseMenuBarAccessibilityID.Detail.launchState.value
            )
        }
    }

    private var issueDetails: some View {
        PanelSection {
            PanelValueRow(
                label: "Service",
                value: snapshot.serviceStatusTitle,
                valueColor: snapshot.serviceStatusLabel == "running" ? snapshot.parsedSeverity.accentColor : .red,
                labelIdentifier: LonghouseMenuBarAccessibilityID.Metric.service.title,
                valueIdentifier: LonghouseMenuBarAccessibilityID.Metric.service.value
            )

            if snapshot.outboxCount > 0 || snapshot.parsedSeverity != .green {
                Divider()

                PanelValueRow(
                    label: "Outbox",
                    value: "\(snapshot.outboxCount)",
                    valueColor: snapshot.outboxCount == 0 ? Color.primary : .orange,
                    labelIdentifier: LonghouseMenuBarAccessibilityID.Metric.outbox.title,
                    valueIdentifier: LonghouseMenuBarAccessibilityID.Metric.outbox.value
                )
            }

            if snapshot.spoolDeadLabel != "0" || snapshot.parsedSeverity == .red {
                Divider()

                PanelValueRow(
                    label: "Dead",
                    value: snapshot.spoolDeadLabel,
                    valueColor: snapshot.spoolDeadLabel == "0" ? Color.primary : .red,
                    labelIdentifier: LonghouseMenuBarAccessibilityID.Metric.dead.title,
                    valueIdentifier: LonghouseMenuBarAccessibilityID.Metric.dead.value
                )
            }
        }
    }

    private var attentionCallout: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Needs attention")
                .font(.system(size: 11, weight: .semibold, design: .rounded))
                .foregroundStyle(snapshot.parsedSeverity.accentColor)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Section.next.title)

            Text(snapshot.attentionSummaryLabel)
                .font(.system(size: 12, weight: .medium, design: .rounded))
                .foregroundStyle(Color.primary)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Section.next.tag(0))
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: sectionCornerRadius, style: .continuous)
                .fill(snapshot.parsedSeverity.accentColor.opacity(0.08))
        )
        .overlay(
            RoundedRectangle(cornerRadius: sectionCornerRadius, style: .continuous)
                .stroke(snapshot.parsedSeverity.accentColor.opacity(0.16), lineWidth: 1)
        )
    }

    private var healthyActions: some View {
        primaryButton(
            title: "Open Longhouse",
            systemImage: "arrow.up.forward.square",
            identifier: LonghouseMenuBarAccessibilityID.Button.openLonghouse,
            tint: Color.accentColor
        ) {
            perform(.openLonghouse)
        }
    }

    private var issueActions: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                primaryButton(
                    title: "Repair",
                    systemImage: "wrench.and.screwdriver",
                    identifier: LonghouseMenuBarAccessibilityID.Button.repair,
                    tint: .red
                ) {
                    perform(.repairInstall)
                }

                secondaryButton(
                    title: "Open Longhouse",
                    systemImage: "arrow.up.forward.square",
                    identifier: LonghouseMenuBarAccessibilityID.Button.openLonghouse
                ) {
                    perform(.openLonghouse)
                }
            }

            HStack(spacing: 8) {
                compactButton(
                    title: "Logs",
                    systemImage: "doc.text.magnifyingglass",
                    identifier: LonghouseMenuBarAccessibilityID.Button.openLogs
                ) {
                    perform(.openLogs)
                }

                compactButton(
                    title: "Copy JSON",
                    systemImage: "doc.on.doc",
                    identifier: LonghouseMenuBarAccessibilityID.Button.copyDiagnostics
                ) {
                    perform(.copyDiagnostics)
                }
            }
        }
    }

    private func primaryButton(
        title: String,
        systemImage: String,
        identifier: String,
        tint: Color,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
        .tint(tint)
        .controlSize(.large)
        .accessibilityIdentifier(identifier)
        .accessibilityLabel(Text(title))
    }

    private func secondaryButton(
        title: String,
        systemImage: String,
        identifier: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.bordered)
        .controlSize(.large)
        .accessibilityIdentifier(identifier)
        .accessibilityLabel(Text(title))
    }

    private func compactButton(
        title: String,
        systemImage: String,
        identifier: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .accessibilityIdentifier(identifier)
        .accessibilityLabel(Text(title))
    }

    private func feedbackBanner(_ feedback: HealthActionFeedback) -> some View {
        let tint = feedbackColor(for: feedback.style)

        return HStack(alignment: .center, spacing: 8) {
            Image(systemName: feedbackIcon(for: feedback.style))
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(tint)

            VStack(alignment: .leading, spacing: 1) {
                Text(feedback.title)
                    .font(.system(size: 11, weight: .semibold, design: .rounded))
                    .foregroundStyle(Color.primary)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.title)

                Text(feedback.detail)
                    .font(.system(size: 10, weight: .medium, design: .rounded))
                    .foregroundStyle(Color.secondary)
                    .lineLimit(2)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.detail)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color(nsColor: .windowBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(tint.opacity(0.24), lineWidth: 1)
        )
        .shadow(color: Color.black.opacity(0.18), radius: 10, x: 0, y: 6)
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.container)
    }

    private func perform(_ action: HarnessAction) {
        feedback = actionSink.handle(action, snapshot: snapshot)
        if action == .refresh {
            refresh()
        }
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
}

private let panelCornerRadius: CGFloat = 18
private let sectionCornerRadius: CGFloat = 12

private struct PanelChrome<Content: View>: View {
    let height: CGFloat
    let content: Content

    init(height: CGFloat, @ViewBuilder content: () -> Content) {
        self.height = height
        self.content = content()
    }

    var body: some View {
        content
            .padding(16)
            .frame(width: MenuBarPanelLayout.panelWidth, height: height, alignment: .topLeading)
            .background(
                RoundedRectangle(cornerRadius: panelCornerRadius, style: .continuous)
                    .fill(Color(nsColor: .windowBackgroundColor))
            )
            .overlay(
                RoundedRectangle(cornerRadius: panelCornerRadius, style: .continuous)
                    .stroke(Color.white.opacity(0.08), lineWidth: 1)
            )
            .shadow(color: Color.black.opacity(0.12), radius: 12, x: 0, y: 8)
    }
}

private struct PanelSection<Content: View>: View {
    let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            content
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: sectionCornerRadius, style: .continuous)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: sectionCornerRadius, style: .continuous)
                .stroke(Color.white.opacity(0.04), lineWidth: 1)
        )
    }
}

private struct PanelValueRow: View {
    let label: String
    let value: String
    var valueColor: Color = .primary
    var labelIdentifier: String? = nil
    var valueIdentifier: String? = nil

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(label)
                .font(.system(size: 12, weight: .medium, design: .rounded))
                .foregroundStyle(Color.secondary)
                .applyHarnessAccessibility(identifier: labelIdentifier, label: label)

            Spacer(minLength: 12)

            Text(value)
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .foregroundStyle(valueColor)
                .multilineTextAlignment(.trailing)
                .applyHarnessAccessibility(identifier: valueIdentifier, label: value)
        }
    }
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
