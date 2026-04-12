import SwiftUI

public struct MenuBarPanelView: View {
    private let snapshot: HealthSnapshot
    private let actionSink: any HealthActionSink
    private let refresh: () -> Void
    @State private var feedback: HealthActionFeedback?
    @State private var showSupportSection: Bool
    @State private var showTechnicalDetails: Bool

    public init(
        snapshot: HealthSnapshot,
        actionSink: any HealthActionSink,
        refresh: @escaping () -> Void
    ) {
        self.snapshot = snapshot
        self.actionSink = actionSink
        self.refresh = refresh
        _feedback = State(initialValue: nil)
        _showSupportSection = State(initialValue: snapshot.parsedSeverity != .green)
        _showTechnicalDetails = State(initialValue: false)
    }

    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if snapshot.updateInfo?.updateAvailable == true {
                    updateBanner
                }
                metrics
                if let feedback {
                    feedbackBanner(feedback)
                }
                actionSection
                technicalDetailsSection
            }
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.panel)
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(width: 420, alignment: .topLeading)
        .frame(minHeight: 360, idealHeight: 560, maxHeight: 760, alignment: .topLeading)
        .background(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(Color(nsColor: .windowBackgroundColor))
                .shadow(color: Color.black.opacity(0.08), radius: 18, x: 0, y: 8)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .stroke(Color.black.opacity(0.05), lineWidth: 1)
        )
        .padding(12)
    }

    private var isHealthy: Bool {
        snapshot.parsedSeverity == .green && snapshot.healthState.lowercased() == "healthy"
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 14) {
            ZStack {
                Circle()
                    .fill(snapshot.parsedSeverity.accentColor.opacity(0.18))
                    .frame(width: 42, height: 42)
                Image(systemName: snapshot.parsedSeverity.symbolName)
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundStyle(snapshot.parsedSeverity.accentColor)
            }
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Header.statusGlyph)

            VStack(alignment: .leading, spacing: 4) {
                Text(snapshot.headline)
                    .font(.system(size: 16, weight: .semibold, design: .rounded))
                    .foregroundStyle(Color.primary)
                    .harnessAccessibility(
                        identifier: LonghouseMenuBarAccessibilityID.Header.headline,
                        label: snapshot.headline
                    )

                Text(snapshot.statusBadge)
                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                    .foregroundStyle(snapshot.parsedSeverity.accentColor)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(snapshot.parsedSeverity.accentColor.opacity(0.12))
                    .clipShape(Capsule())
                    .harnessAccessibility(
                        identifier: LonghouseMenuBarAccessibilityID.Header.statusBadge,
                        label: snapshot.statusBadge
                    )

                Text("Last ship: \(snapshot.lastShipLabel)")
                    .font(.system(size: 12, weight: .medium, design: .rounded))
                    .foregroundStyle(Color.secondary)
                    .harnessAccessibility(
                        identifier: LonghouseMenuBarAccessibilityID.Header.lastShip,
                        label: "Last ship: \(snapshot.lastShipLabel)"
                    )

                if let launchHeadline = snapshot.launchReadiness?.headline,
                   !launchHeadline.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    Text(launchHeadline)
                        .font(.system(size: 12, weight: .medium, design: .rounded))
                        .foregroundStyle(Color.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private var updateBanner: some View {
        HStack(spacing: 10) {
            Image(systemName: "arrow.up.circle.fill")
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(Color.blue)

            VStack(alignment: .leading, spacing: 2) {
                let installed = snapshot.updateInfo?.installedVersion ?? ""
                let latest = snapshot.updateInfo?.latestVersion?.trimmingCharacters(in: .whitespacesAndNewlines)
                let label = if let latest, !latest.isEmpty {
                    "Longhouse \(latest) available (you have \(installed))"
                } else {
                    "A Longhouse update is available (you have \(installed))"
                }
                Text(label)
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .foregroundStyle(Color.primary)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.UpdateBanner.label)
            }

            Spacer()

            Button("Upgrade") {
                feedback = actionSink.handle(.upgradeNow, snapshot: snapshot)
            }
            .buttonStyle(.plain)
            .font(.system(size: 11, weight: .bold, design: .rounded))
            .foregroundStyle(Color.white)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(Color.blue)
            .clipShape(Capsule())
            .harnessAccessibilityButton(
                identifier: LonghouseMenuBarAccessibilityID.Button.upgradeNow,
                label: "Upgrade"
            )
        }
        .padding(12)
        .background(Color.blue.opacity(0.10))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.UpdateBanner.container)
    }

    private var metrics: some View {
        HStack(spacing: 10) {
            metricCard(title: "Service", value: snapshot.serviceStatusLabel, tint: Color.blue, metric: .service)
            metricCard(title: "Engine Age", value: snapshot.engineAgeLabel, tint: Color.indigo, metric: .engineAge)
            metricCard(title: "Outbox", value: "\(snapshot.outboxCount)", tint: Color.teal, metric: .outbox)
            metricCard(title: "Dead", value: snapshot.spoolDeadLabel, tint: snapshot.parsedSeverity.accentColor, metric: .dead)
        }
    }

    private func metricCard(
        title: String,
        value: String,
        tint: Color,
        metric: LonghouseMenuBarAccessibilityID.Metric
    ) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title.uppercased())
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundStyle(Color.secondary)
                .harnessAccessibility(
                    identifier: metric.title,
                    label: title.uppercased()
                )
            Text(value)
                .font(.system(size: 15, weight: .semibold, design: .rounded))
                .foregroundStyle(Color.primary)
                .harnessAccessibility(
                    identifier: metric.value,
                    label: value
                )
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(tint.opacity(0.10))
        )
    }

    @ViewBuilder
    private var actionSection: some View {
        if isHealthy {
            healthyActionSection
        } else {
            troubleshootingSection
        }
    }

    private var healthyActionSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Divider()

            sectionEyebrow("Primary")

            Text("Longhouse looks healthy on this Mac. Open the dashboard or leave this running quietly in the menu bar.")
                .font(.system(size: 12, weight: .medium, design: .rounded))
                .foregroundStyle(Color.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 8) {
                controlButton("Open Longhouse", systemImage: "arrow.up.forward.square", tone: .primary) {
                    perform(.openLonghouse)
                }
                .harnessAccessibilityButton(
                    identifier: LonghouseMenuBarAccessibilityID.Button.openLonghouse,
                    label: "Open Longhouse"
                )

                controlButton("Refresh", systemImage: "arrow.clockwise", tone: .secondary) {
                    perform(.refresh)
                }
                .harnessAccessibilityButton(
                    identifier: LonghouseMenuBarAccessibilityID.Button.refresh,
                    label: "Refresh"
                )
            }

            DisclosureGroup(isExpanded: $showSupportSection) {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Doctor is read-only. Repair can update the local app, service wiring, and automatic imports on this Mac.")
                        .font(.system(size: 11, weight: .medium, design: .rounded))
                        .foregroundStyle(Color.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    HStack(spacing: 8) {
                        controlButton("Doctor", systemImage: "stethoscope", tone: .secondary) {
                            perform(.runDoctor)
                        }
                        .harnessAccessibilityButton(
                            identifier: LonghouseMenuBarAccessibilityID.Button.doctor,
                            label: "Doctor"
                        )

                        controlButton("Logs", systemImage: "doc.text.magnifyingglass", tone: .secondary) {
                            perform(.openLogs)
                        }
                        .harnessAccessibilityButton(
                            identifier: LonghouseMenuBarAccessibilityID.Button.openLogs,
                            label: "Logs"
                        )

                        controlButton("Repair", systemImage: "wrench.and.screwdriver", tone: .warning) {
                            perform(.repairInstall)
                        }
                        .harnessAccessibilityButton(
                            identifier: LonghouseMenuBarAccessibilityID.Button.repair,
                            label: "Repair"
                        )
                    }

                    controlButton("Copy JSON", systemImage: "doc.on.doc", tone: .secondary) {
                        perform(.copyDiagnostics)
                    }
                    .harnessAccessibilityButton(
                        identifier: LonghouseMenuBarAccessibilityID.Button.copyDiagnostics,
                        label: "Copy JSON"
                    )
                }
                .padding(.top, 10)
            } label: {
                sectionDisclosureLabel("Maintenance & diagnostics")
            }
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Disclosure.troubleshooting)
        }
    }

    private var troubleshootingSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Divider()

            sectionEyebrow("Needs attention")

            if let firstSuggestedAction = snapshot.suggestedActions.first {
                Text(firstSuggestedAction)
                    .font(.system(size: 13, weight: .semibold, design: .rounded))
                    .foregroundStyle(Color.primary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(
                        RoundedRectangle(cornerRadius: 12, style: .continuous)
                            .fill(snapshot.parsedSeverity.accentColor.opacity(0.10))
                    )
            }

            Text("Repair is the fastest way to rewire the local runtime. Doctor is safer when you want to inspect before changing anything.")
                .font(.system(size: 12, weight: .medium, design: .rounded))
                .foregroundStyle(Color.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 8) {
                controlButton("Repair", systemImage: "wrench.and.screwdriver", tone: .danger) {
                    perform(.repairInstall)
                }
                .harnessAccessibilityButton(
                    identifier: LonghouseMenuBarAccessibilityID.Button.repair,
                    label: "Repair"
                )

                controlButton("Doctor", systemImage: "stethoscope", tone: .secondary) {
                    perform(.runDoctor)
                }
                .harnessAccessibilityButton(
                    identifier: LonghouseMenuBarAccessibilityID.Button.doctor,
                    label: "Doctor"
                )

                controlButton("Logs", systemImage: "doc.text.magnifyingglass", tone: .secondary) {
                    perform(.openLogs)
                }
                .harnessAccessibilityButton(
                    identifier: LonghouseMenuBarAccessibilityID.Button.openLogs,
                    label: "Logs"
                )
            }

            HStack(spacing: 8) {
                controlButton("Refresh", systemImage: "arrow.clockwise", tone: .secondary) {
                    perform(.refresh)
                }
                .harnessAccessibilityButton(
                    identifier: LonghouseMenuBarAccessibilityID.Button.refresh,
                    label: "Refresh"
                )

                controlButton("Open Longhouse", systemImage: "arrow.up.forward.square", tone: .secondary) {
                    perform(.openLonghouse)
                }
                .harnessAccessibilityButton(
                    identifier: LonghouseMenuBarAccessibilityID.Button.openLonghouse,
                    label: "Open Longhouse"
                )

                controlButton("Copy JSON", systemImage: "doc.on.doc", tone: .secondary) {
                    perform(.copyDiagnostics)
                }
                .harnessAccessibilityButton(
                    identifier: LonghouseMenuBarAccessibilityID.Button.copyDiagnostics,
                    label: "Copy JSON"
                )
            }

            DisclosureGroup(isExpanded: $showSupportSection) {
                VStack(alignment: .leading, spacing: 10) {
                    if !snapshot.reasons.isEmpty {
                        tagSection(
                            title: "Reasons",
                            values: snapshot.reasons,
                            color: snapshot.parsedSeverity.accentColor,
                            section: .reasons
                        )
                    }

                    if !snapshot.suggestedActions.isEmpty {
                        suggestionList(snapshot.suggestedActions)
                    }
                }
                .padding(.top, 10)
            } label: {
                sectionDisclosureLabel("Troubleshooting details")
            }
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Disclosure.troubleshooting)
        }
    }

    private var technicalDetailsSection: some View {
        DisclosureGroup(isExpanded: $showTechnicalDetails) {
            VStack(alignment: .leading, spacing: 10) {
                labeledRow(label: "Service File", value: snapshot.service?.serviceFile ?? "-", detail: .serviceFile)
                labeledRow(label: "Log Path", value: snapshot.service?.logPath ?? "-", detail: .logPath)
                labeledRow(label: "Spool Pending", value: snapshot.spoolPendingLabel, detail: .spoolPending)
                labeledRow(label: "Outbox Oldest", value: snapshot.outboxOldestLabel, detail: .outboxOldest)
                labeledRow(label: "Launch State", value: snapshot.launchStateLabel, detail: .launchState)
                labeledRow(label: "Machine / Runner", value: snapshot.machineRunnerLabel, detail: .machineRunner)
                labeledRow(label: "Service Machine", value: snapshot.serviceMachineLabel, detail: .serviceMachine)
                labeledRow(label: "Stored / Runner URL", value: snapshot.storedRunnerURLLabel, detail: .storedRunnerURL)

                if let launchReasons = snapshot.launchReadiness?.reasons, !launchReasons.isEmpty {
                    tagSection(
                        title: "Launch checks",
                        values: launchReasons,
                        color: snapshot.parsedSeverity.accentColor,
                        section: .launchChecks
                    )
                }
            }
            .padding(.top, 10)
        } label: {
            sectionDisclosureLabel("Technical details")
        }
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Disclosure.technicalDetails)
    }

    private func feedbackBanner(_ feedback: HealthActionFeedback) -> some View {
        let tint = feedbackColor(for: feedback.style)

        return HStack(alignment: .top, spacing: 10) {
            Image(systemName: feedbackIcon(for: feedback.style))
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(tint)

            VStack(alignment: .leading, spacing: 3) {
                Text(feedback.title)
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .foregroundStyle(Color.primary)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.title)

                Text(feedback.detail)
                    .font(.system(size: 11, weight: .medium, design: .rounded))
                    .foregroundStyle(Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.detail)
            }
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(tint.opacity(0.10))
        )
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.container)
    }

    private func labeledRow(
        label: String,
        value: String,
        detail: LonghouseMenuBarAccessibilityID.Detail
    ) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label.uppercased())
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundStyle(Color.secondary)
                .harnessAccessibility(
                    identifier: detail.label,
                    label: label.uppercased()
                )
            Text(value)
                .font(.system(size: 12, weight: .medium, design: .rounded))
                .foregroundStyle(Color.primary)
                .textSelection(.enabled)
                .harnessAccessibility(
                    identifier: detail.value,
                    label: value
                )
        }
    }

    private func tagSection(
        title: String,
        values: [String],
        color: Color,
        section: LonghouseMenuBarAccessibilityID.Section
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title.uppercased())
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundStyle(Color.secondary)
                .harnessAccessibility(
                    identifier: section.title,
                    label: title.uppercased()
                )

            FlowLayout(values: values, color: color, section: section)
        }
    }

    private func suggestionList(_ values: [String]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("NEXT")
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundStyle(Color.secondary)
                .harnessAccessibility(
                    identifier: LonghouseMenuBarAccessibilityID.Section.next.title,
                    label: "NEXT"
                )

            ForEach(Array(values.enumerated()), id: \.offset) { index, value in
                HStack(alignment: .top, spacing: 8) {
                    Circle()
                        .fill(Color.blue)
                        .frame(width: 5, height: 5)
                        .padding(.top, 5)
                    Text(value)
                        .font(.system(size: 11, weight: .medium, design: .rounded))
                        .foregroundStyle(Color.primary)
                        .fixedSize(horizontal: false, vertical: true)
                        .harnessAccessibility(
                            identifier: LonghouseMenuBarAccessibilityID.Section.next.tag(index),
                            label: value
                        )
                }
            }
        }
    }

    private func sectionEyebrow(_ title: String) -> some View {
        Text(title.uppercased())
            .font(.system(size: 9, weight: .bold, design: .monospaced))
            .foregroundStyle(Color.secondary)
    }

    private func sectionDisclosureLabel(_ title: String) -> some View {
        HStack {
            Text(title)
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .foregroundStyle(Color.primary)
            Spacer()
        }
        .contentShape(Rectangle())
    }

    private func perform(_ action: HarnessAction) {
        feedback = actionSink.handle(action, snapshot: snapshot)
        if action == .refresh {
            refresh()
        }
    }

    private func controlButton(
        _ title: String,
        systemImage: String,
        tone: ControlButtonTone,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.plain)
        .foregroundStyle(tone.foregroundColor)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(tone.backgroundColor)
        )
    }

    private func feedbackColor(for style: HealthActionFeedbackStyle) -> Color {
        switch style {
        case .info:
            return Color.blue
        case .success:
            return snapshot.parsedSeverity == .green ? snapshot.parsedSeverity.accentColor : Color.green
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

private enum ControlButtonTone {
    case primary
    case secondary
    case warning
    case danger

    var backgroundColor: Color {
        switch self {
        case .primary:
            return Color.blue
        case .secondary:
            return Color.black.opacity(0.06)
        case .warning:
            return Color.orange.opacity(0.16)
        case .danger:
            return Color.red.opacity(0.16)
        }
    }

    var foregroundColor: Color {
        switch self {
        case .primary:
            return Color.white
        case .secondary:
            return Color.primary
        case .warning:
            return Color.orange
        case .danger:
            return Color.red
        }
    }
}

private struct FlowLayout: View {
    let values: [String]
    let color: Color
    let section: LonghouseMenuBarAccessibilityID.Section

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(values.enumerated()), id: \.offset) { index, value in
                Text(value)
                    .font(.system(size: 11, weight: .semibold, design: .rounded))
                    .foregroundStyle(color)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 5)
                    .background(color.opacity(0.12))
                    .clipShape(Capsule())
                    .harnessAccessibility(
                        identifier: section.tag(index),
                        label: value
                    )
            }
        }
    }
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
}
