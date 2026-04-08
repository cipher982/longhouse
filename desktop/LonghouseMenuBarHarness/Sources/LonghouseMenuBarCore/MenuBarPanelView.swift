import SwiftUI

public struct MenuBarPanelView: View {
    private let snapshot: HealthSnapshot
    private let actionSink: any HealthActionSink
    private let refresh: () -> Void

    public init(
        snapshot: HealthSnapshot,
        actionSink: any HealthActionSink,
        refresh: @escaping () -> Void
    ) {
        self.snapshot = snapshot
        self.actionSink = actionSink
        self.refresh = refresh
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header
            metrics
            detailBlocks
            controls
        }
        .padding(18)
        .frame(width: 420, alignment: .leading)
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
            }
        }
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

    private var detailBlocks: some View {
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
                    title: "Launch Checks",
                    values: launchReasons,
                    color: snapshot.parsedSeverity.accentColor,
                    section: .launchChecks
                )
            }

            if !snapshot.reasons.isEmpty {
                tagSection(
                    title: "Reasons",
                    values: snapshot.reasons,
                    color: snapshot.parsedSeverity.accentColor,
                    section: .reasons
                )
            }

            if !snapshot.suggestedActions.isEmpty {
                tagSection(
                    title: "Next",
                    values: snapshot.suggestedActions,
                    color: Color.blue,
                    section: .next
                )
            }
        }
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

    private var controls: some View {
        VStack(alignment: .leading, spacing: 10) {
            Divider()
            HStack(spacing: 8) {
                controlButton("Refresh", systemImage: "arrow.clockwise") {
                    actionSink.handle(.refresh, snapshot: snapshot)
                    refresh()
                }
                .harnessAccessibilityButton(
                    identifier: LonghouseMenuBarAccessibilityID.Button.refresh,
                    label: "Refresh"
                )

                controlButton("Doctor", systemImage: "stethoscope") {
                    actionSink.handle(.runDoctor, snapshot: snapshot)
                }
                .harnessAccessibilityButton(
                    identifier: LonghouseMenuBarAccessibilityID.Button.doctor,
                    label: "Doctor"
                )

                controlButton("Repair", systemImage: "wrench.and.screwdriver") {
                    actionSink.handle(.repairInstall, snapshot: snapshot)
                }
                .harnessAccessibilityButton(
                    identifier: LonghouseMenuBarAccessibilityID.Button.repair,
                    label: "Repair"
                )
            }

            HStack(spacing: 8) {
                controlButton("Copy JSON", systemImage: "doc.on.doc") {
                    actionSink.handle(.copyDiagnostics, snapshot: snapshot)
                }
                .harnessAccessibilityButton(
                    identifier: LonghouseMenuBarAccessibilityID.Button.copyDiagnostics,
                    label: "Copy JSON"
                )

                controlButton("Logs", systemImage: "doc.text.magnifyingglass") {
                    actionSink.handle(.openLogs, snapshot: snapshot)
                }
                .harnessAccessibilityButton(
                    identifier: LonghouseMenuBarAccessibilityID.Button.openLogs,
                    label: "Logs"
                )

                controlButton("Open Longhouse", systemImage: "arrow.up.forward.square") {
                    actionSink.handle(.openLonghouse, snapshot: snapshot)
                }
                .harnessAccessibilityButton(
                    identifier: LonghouseMenuBarAccessibilityID.Button.openLonghouse,
                    label: "Open Longhouse"
                )
            }
        }
    }

    private func controlButton(_ title: String, systemImage: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.plain)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color.black.opacity(0.06))
        )
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
