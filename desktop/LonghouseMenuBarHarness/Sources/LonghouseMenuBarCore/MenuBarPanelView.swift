import SwiftUI

public enum MenuBarPanelLayout {
    public static let panelWidth: CGFloat = 368
    public static let loadingHeight: CGFloat = 220
    public static let failureHeight: CGFloat = 248
    public static let healthyHeight: CGFloat = 376
    public static let attentionHeight: CGFloat = 468

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
        VStack(alignment: .leading, spacing: 14) {
            topAccent(.gray)

            Spacer(minLength: 0)

            VStack(alignment: .leading, spacing: 10) {
                ProgressView()
                    .controlSize(.small)

                Text("Loading local health")
                    .font(.system(size: 16, weight: .semibold, design: .rounded))
                    .foregroundStyle(Color.primary)

                Text("Longhouse is checking shipping status on this Mac.")
                    .font(.system(size: 12, weight: .medium, design: .rounded))
                    .foregroundStyle(Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Spacer(minLength: 0)
        }
        .padding(16)
        .frame(width: MenuBarPanelLayout.panelWidth, height: MenuBarPanelLayout.loadingHeight, alignment: .topLeading)
        .background(Color(nsColor: .windowBackgroundColor))
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
        VStack(alignment: .leading, spacing: 14) {
            topAccent(.red)

            Spacer(minLength: 0)

            VStack(alignment: .leading, spacing: 10) {
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

            PanelActionButton(
                title: "Retry",
                systemImage: "arrow.clockwise",
                tone: .primary,
                accessibilityIdentifier: LonghouseMenuBarAccessibilityID.Error.retryButton,
                accessibilityLabel: "Retry",
                action: retry
            )
        }

            Spacer(minLength: 0)
        }
        .padding(16)
        .frame(width: MenuBarPanelLayout.panelWidth, height: MenuBarPanelLayout.failureHeight, alignment: .topLeading)
        .background(Color(nsColor: .windowBackgroundColor))
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
        VStack(alignment: .leading, spacing: 14) {
            topAccent(snapshot.parsedSeverity.accentColor)
            header

            if snapshot.updateInfo?.updateAvailable == true {
                updateBanner
            }

            summaryLines
            metrics

            if !isHealthy {
                attentionCard
            }

            Spacer(minLength: 0)

            primaryActions
            secondaryActions
        }
        .padding(16)
        .frame(
            width: MenuBarPanelLayout.panelWidth,
            height: MenuBarPanelLayout.preferredHeight(for: snapshot),
            alignment: .topLeading
        )
        .background(Color(nsColor: .windowBackgroundColor))
        .overlay(alignment: .top) {
            if let feedback {
                feedbackBanner(feedback)
                    .padding(.horizontal, 16)
                    .padding(.top, 68)
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

            VStack(alignment: .leading, spacing: 6) {
                Text(snapshot.headline)
                    .font(.system(size: 16, weight: .semibold, design: .rounded))
                    .foregroundStyle(Color.primary)
                    .fixedSize(horizontal: false, vertical: true)
                    .harnessAccessibility(
                        identifier: LonghouseMenuBarAccessibilityID.Header.headline,
                        label: snapshot.headline
                    )

                HStack(spacing: 8) {
                    Text(snapshot.ambientStatusLabel.uppercased())
                        .font(.system(size: 10, weight: .black, design: .monospaced))
                        .foregroundStyle(snapshot.parsedSeverity.accentColor)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(snapshot.parsedSeverity.accentColor.opacity(0.12))
                        .clipShape(Capsule())
                        .harnessAccessibility(
                            identifier: LonghouseMenuBarAccessibilityID.Header.statusBadge,
                            label: snapshot.ambientStatusLabel
                        )

                    if isRefreshing {
                        HStack(spacing: 5) {
                            ProgressView()
                                .controlSize(.mini)
                            Text("Refreshing")
                                .font(.system(size: 10, weight: .semibold, design: .rounded))
                        }
                        .foregroundStyle(Color.secondary)
                    }
                }
            }

            Spacer(minLength: 0)
        }
    }

    private var updateBanner: some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: "arrow.up.circle.fill")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(Color.blue)

            VStack(alignment: .leading, spacing: 2) {
                let latest = snapshot.updateInfo?.latestVersion?.trimmingCharacters(in: .whitespacesAndNewlines)
                let installed = snapshot.updateInfo?.installedVersion ?? ""
                let label = if let latest, !latest.isEmpty {
                    "Longhouse \(latest) is ready. You have \(installed)."
                } else {
                    "A Longhouse update is ready."
                }

                Text(label)
                    .font(.system(size: 11, weight: .semibold, design: .rounded))
                    .foregroundStyle(Color.primary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.UpdateBanner.label)
            }

            Spacer(minLength: 0)

            PanelActionButton(
                title: "Upgrade",
                systemImage: "arrow.down",
                tone: .accent(.blue),
                compact: true,
                accessibilityIdentifier: LonghouseMenuBarAccessibilityID.Button.upgradeNow,
                accessibilityLabel: "Upgrade"
            ) {
                feedback = actionSink.handle(.upgradeNow, snapshot: snapshot)
            }
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Color.blue.opacity(0.10))
        )
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.UpdateBanner.container)
    }

    private var summaryLines: some View {
        VStack(alignment: .leading, spacing: 6) {
            summaryLine(
                systemImage: "paperplane.circle.fill",
                text: snapshot.lastShipSummaryLabel,
                identifier: LonghouseMenuBarAccessibilityID.Header.lastShip
            )

            summaryLine(
                systemImage: snapshot.launchStateLabel == "ready" ? "bolt.circle.fill" : "bolt.trianglebadge.exclamationmark.fill",
                text: snapshot.launchSummaryLabel
            )
        }
    }

    private var metrics: some View {
        HStack(spacing: 8) {
            MetricTile(
                title: "Service",
                value: snapshot.serviceStatusTitle,
                tint: snapshot.serviceStatusLabel == "running" ? .blue : .red,
                titleIdentifier: LonghouseMenuBarAccessibilityID.Metric.service.title,
                valueIdentifier: LonghouseMenuBarAccessibilityID.Metric.service.value
            )

            MetricTile(
                title: "Outbox",
                value: "\(snapshot.outboxCount)",
                tint: snapshot.outboxCount == 0 ? .teal : snapshot.parsedSeverity.accentColor,
                titleIdentifier: LonghouseMenuBarAccessibilityID.Metric.outbox.title,
                valueIdentifier: LonghouseMenuBarAccessibilityID.Metric.outbox.value
            )

            MetricTile(
                title: "Dead",
                value: snapshot.spoolDeadLabel,
                tint: snapshot.spoolDeadLabel == "0" ? .green : .red,
                titleIdentifier: LonghouseMenuBarAccessibilityID.Metric.dead.title,
                valueIdentifier: LonghouseMenuBarAccessibilityID.Metric.dead.value
            )
        }
    }

    private var attentionCard: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("NEXT")
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundStyle(snapshot.parsedSeverity.accentColor)
                .harnessAccessibility(
                    identifier: LonghouseMenuBarAccessibilityID.Section.next.title,
                    label: "NEXT"
                )

            Text(snapshot.attentionSummaryLabel)
                .font(.system(size: 12, weight: .medium, design: .rounded))
                .foregroundStyle(Color.primary)
                .fixedSize(horizontal: false, vertical: true)
                .harnessAccessibility(
                    identifier: LonghouseMenuBarAccessibilityID.Section.next.tag(0),
                    label: snapshot.attentionSummaryLabel
                )
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(snapshot.parsedSeverity.accentColor.opacity(0.12))
        )
    }

    private var primaryActions: some View {
        HStack(spacing: 8) {
            if isHealthy {
                PanelActionButton(
                    title: "Open Longhouse",
                    systemImage: "arrow.up.forward.square",
                    tone: .primary,
                    accessibilityIdentifier: LonghouseMenuBarAccessibilityID.Button.openLonghouse,
                    accessibilityLabel: "Open Longhouse"
                ) {
                    perform(.openLonghouse)
                }

                PanelActionButton(
                    title: isRefreshing ? "Refreshing" : "Refresh",
                    systemImage: "arrow.clockwise",
                    tone: .secondary,
                    isBusy: isRefreshing,
                    accessibilityIdentifier: LonghouseMenuBarAccessibilityID.Button.refresh,
                    accessibilityLabel: "Refresh"
                ) {
                    perform(.refresh)
                }
                .disabled(isRefreshing)
            } else {
                PanelActionButton(
                    title: "Repair",
                    systemImage: "wrench.and.screwdriver",
                    tone: .danger,
                    accessibilityIdentifier: LonghouseMenuBarAccessibilityID.Button.repair,
                    accessibilityLabel: "Repair"
                ) {
                    perform(.repairInstall)
                }

                PanelActionButton(
                    title: "Doctor",
                    systemImage: "stethoscope",
                    tone: .secondary,
                    accessibilityIdentifier: LonghouseMenuBarAccessibilityID.Button.doctor,
                    accessibilityLabel: "Doctor"
                ) {
                    perform(.runDoctor)
                }
            }
        }
    }

    private var secondaryActions: some View {
        VStack(spacing: 8) {
            if isHealthy {
                HStack(spacing: 8) {
                    secondaryButton("Doctor", systemImage: "stethoscope", action: .runDoctor, identifier: LonghouseMenuBarAccessibilityID.Button.doctor)
                    secondaryButton("Repair", systemImage: "wrench.and.screwdriver", action: .repairInstall, identifier: LonghouseMenuBarAccessibilityID.Button.repair)
                }

                HStack(spacing: 8) {
                    secondaryButton("Logs", systemImage: "doc.text.magnifyingglass", action: .openLogs, identifier: LonghouseMenuBarAccessibilityID.Button.openLogs)
                    secondaryButton("Copy JSON", systemImage: "doc.on.doc", action: .copyDiagnostics, identifier: LonghouseMenuBarAccessibilityID.Button.copyDiagnostics)
                }
            } else {
                HStack(spacing: 8) {
                    secondaryButton("Copy JSON", systemImage: "doc.on.doc", action: .copyDiagnostics, identifier: LonghouseMenuBarAccessibilityID.Button.copyDiagnostics)
                    secondaryButton("Logs", systemImage: "doc.text.magnifyingglass", action: .openLogs, identifier: LonghouseMenuBarAccessibilityID.Button.openLogs)
                }

                HStack(spacing: 8) {
                    secondaryButton(
                        isRefreshing ? "Refreshing" : "Refresh",
                        systemImage: "arrow.clockwise",
                        action: .refresh,
                        identifier: LonghouseMenuBarAccessibilityID.Button.refresh,
                        isDisabled: isRefreshing,
                        isBusy: isRefreshing
                    )
                    secondaryButton("Open Longhouse", systemImage: "arrow.up.forward.square", action: .openLonghouse, identifier: LonghouseMenuBarAccessibilityID.Button.openLonghouse)
                }
            }
        }
    }

    private func secondaryButton(
        _ title: String,
        systemImage: String,
        action: HarnessAction,
        identifier: String,
        isDisabled: Bool = false,
        isBusy: Bool = false
    ) -> some View {
        PanelActionButton(
            title: title,
            systemImage: systemImage,
            tone: .secondary,
            compact: true,
            isBusy: isBusy,
            accessibilityIdentifier: identifier,
            accessibilityLabel: title
        ) {
            perform(action)
        }
        .disabled(isDisabled)
    }

    private func summaryLine(systemImage: String, text: String, identifier: String? = nil) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: systemImage)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Color.secondary)

            Text(text)
                .font(.system(size: 11, weight: .medium, design: .rounded))
                .foregroundStyle(Color.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .applyHarnessAccessibility(identifier: identifier, label: text)
        }
    }

    private func feedbackBanner(_ feedback: HealthActionFeedback) -> some View {
        let tint = feedbackColor(for: feedback.style)

        return HStack(alignment: .top, spacing: 8) {
            Image(systemName: feedbackIcon(for: feedback.style))
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(tint)

            VStack(alignment: .leading, spacing: 3) {
                Text(feedback.title)
                    .font(.system(size: 11, weight: .semibold, design: .rounded))
                    .foregroundStyle(Color.primary)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.title)

                Text(feedback.detail)
                    .font(.system(size: 10, weight: .medium, design: .rounded))
                    .foregroundStyle(Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.detail)
            }
        }
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(tint.opacity(0.10))
        )
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
            return Color.blue
        case .success:
            return Color.green
        case .warning:
            return Color.orange
        case .failure:
            return Color.red
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

private struct MetricTile: View {
    let title: String
    let value: String
    let tint: Color
    let titleIdentifier: String
    let valueIdentifier: String

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title.uppercased())
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundStyle(Color.secondary)
                .harnessAccessibility(identifier: titleIdentifier, label: title.uppercased())

            Text(value)
                .font(.system(size: 14, weight: .semibold, design: .rounded))
                .foregroundStyle(Color.primary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .harnessAccessibility(identifier: valueIdentifier, label: value)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(tint.opacity(0.10))
        )
    }
}

private struct PanelActionButton: View {
    let title: String
    let systemImage: String
    let tone: PanelActionTone
    var compact: Bool = false
    var isBusy: Bool = false
    var accessibilityIdentifier: String? = nil
    var accessibilityLabel: String? = nil
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 7) {
                if isBusy {
                    ProgressView()
                        .controlSize(.mini)
                } else {
                    Image(systemName: systemImage)
                        .font(.system(size: compact ? 11 : 12, weight: .semibold))
                }

                Text(title)
                    .font(.system(size: compact ? 11 : 12, weight: .semibold, design: .rounded))
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity)
            .padding(.horizontal, compact ? 10 : 12)
            .padding(.vertical, compact ? 8 : 10)
        }
        .buttonStyle(.plain)
        .applyPanelAccessibility(
            identifier: accessibilityIdentifier,
            label: accessibilityLabel ?? title
        )
        .foregroundStyle(tone.foregroundColor)
        .background(
            RoundedRectangle(cornerRadius: compact ? 10 : 12, style: .continuous)
                .fill(tone.backgroundColor)
        )
    }
}

private enum PanelActionTone {
    case primary
    case secondary
    case danger
    case accent(Color)

    var backgroundColor: Color {
        switch self {
        case .primary:
            return Color.accentColor
        case .secondary:
            return Color.primary.opacity(0.07)
        case .danger:
            return Color.red
        case let .accent(color):
            return color
        }
    }

    var foregroundColor: Color {
        switch self {
        case .primary, .danger, .accent:
            return Color.white
        case .secondary:
            return Color.primary
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

private func topAccent(_ color: Color) -> some View {
    RoundedRectangle(cornerRadius: 999, style: .continuous)
        .fill(
            LinearGradient(
                colors: [color.opacity(0.85), color.opacity(0.30)],
                startPoint: .leading,
                endPoint: .trailing
            )
        )
        .frame(height: 4)
}

private extension View {
    func harnessAccessibility(identifier: String, label: String) -> some View {
        accessibilityIdentifier(identifier)
            .accessibilityLabel(Text(label))
    }

    func harnessAccessibilityButton(identifier: String, label: String) -> some View {
        accessibilityIdentifier(identifier)
            .accessibilityLabel(Text(label))
    }

    @ViewBuilder
    func applyHarnessAccessibility(identifier: String?, label: String) -> some View {
        if let identifier {
            harnessAccessibility(identifier: identifier, label: label)
        } else {
            self
        }
    }

    @ViewBuilder
    func applyPanelAccessibility(identifier: String?, label: String) -> some View {
        if let identifier {
            accessibilityIdentifier(identifier)
                .accessibilityLabel(Text(label))
        } else {
            self
        }
    }
}
