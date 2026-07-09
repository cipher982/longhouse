import AppKit
import SwiftUI

struct PanelTelemetryEntry: Identifiable {
    let id: String
    let label: String
    let value: String
    let valueColor: Color
    let labelIdentifier: String?
    let valueIdentifier: String?

    init(
        id: String? = nil,
        label: String,
        value: String,
        valueColor: Color = .primary,
        labelIdentifier: String? = nil,
        valueIdentifier: String? = nil
    ) {
        self.id = id ?? label
        self.label = label
        self.value = value
        self.valueColor = valueColor
        self.labelIdentifier = labelIdentifier
        self.valueIdentifier = valueIdentifier
    }
}

struct PanelReadout: Identifiable {
    let id: String
    let label: String
    let value: String
    let detail: String
    let tone: Color

    init(id: String? = nil, label: String, value: String, detail: String, tone: Color = .primary) {
        self.id = id ?? label
        self.label = label
        self.value = value
        self.detail = detail
        self.tone = tone
    }
}

struct PanelChrome<Content: View>: View {
    let accent: Color
    let content: Content

    init(accent: Color, @ViewBuilder content: () -> Content) {
        self.accent = accent
        self.content = content()
    }

    var body: some View {
        ZStack(alignment: .topLeading) {
            PanelMaterialBackground(cornerRadius: MenuBarPanelLayout.chromeCornerRadius)

            LinearGradient(
                colors: [accent.opacity(0.04), Color.clear],
                startPoint: .top,
                endPoint: .bottom
            )

            content
                .padding(.leading, MenuBarPanelLayout.chromeHorizontalPadding)
                .padding(.trailing, MenuBarPanelLayout.chromeHorizontalPadding)
                .padding(.bottom, MenuBarPanelLayout.chromeBottomPadding)
                .padding(.top, MenuBarPanelLayout.chromeTopContentInset)
        }
        .frame(width: MenuBarPanelLayout.panelWidth, alignment: .topLeading)
        .fixedSize(horizontal: false, vertical: true)
        .clipShape(RoundedRectangle(cornerRadius: MenuBarPanelLayout.chromeCornerRadius, style: .continuous))
        .overlay(alignment: .top) {
            RoundedRectangle(cornerRadius: MenuBarPanelLayout.chromeCornerRadius, style: .continuous)
                .fill(accent.opacity(0.72))
                .frame(height: MenuBarPanelLayout.accentHeight)
                .padding(.top, MenuBarPanelLayout.chromeTopRailInset)
        }
    }
}

struct PanelMaterialBackground: NSViewRepresentable {
    let cornerRadius: CGFloat

    func makeNSView(context: Context) -> NSView {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *) {
            let glass = NSGlassEffectView()
            glass.cornerRadius = cornerRadius
            return glass
        }
        #endif

        let view = NSVisualEffectView()
        view.material = .popover
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *) {
            (nsView as? NSGlassEffectView)?.cornerRadius = cornerRadius
        }
        #endif
    }
}

struct PanelSection<Content: View>: View {
    let title: String
    let trailing: String?
    let content: Content

    init(title: String, trailing: String? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.trailing = trailing
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: MenuBarPanelLayout.sectionHeaderSpacing) {
            HStack(alignment: .center, spacing: 8) {
                deckColumnTitle(title)

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
        .padding(MenuBarPanelLayout.sectionInsets)
    }
}

struct TelemetryTable: View {
    let entries: [PanelTelemetryEntry]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(entries.enumerated()), id: \.element.id) { index, entry in
                LabeledContent {
                    Text(entry.value)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(entry.valueColor)
                        .monospacedDigit()
                        .multilineTextAlignment(.trailing)
                        .lineLimit(2)
                        .minimumScaleFactor(0.8)
                        .applyHarnessAccessibility(identifier: entry.valueIdentifier, label: entry.value)
                } label: {
                    Text(entry.label)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Color.secondary)
                        .applyHarnessAccessibility(identifier: entry.labelIdentifier, label: entry.label)
                }
                .padding(.vertical, 6)

                if index < entries.count - 1 {
                    sectionDivider
                }
            }
        }
    }
}

struct MissionReadoutGrid: View {
    let readouts: [PanelReadout]

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            ForEach(Array(readouts.enumerated()), id: \.element.id) { index, readout in
                MissionReadoutCell(readout: readout)

                if index < readouts.count - 1 {
                    Rectangle()
                        .fill(Color.white.opacity(0.07))
                        .frame(width: 1)
                        .padding(.vertical, 10)
                }
            }
        }
        .padding(.horizontal, 2)
        .padding(.vertical, 2)
    }
}

struct HeaderRailMetric: Identifiable {
    let id: String
    let label: String
    let value: String
    let tint: Color

    init(id: String? = nil, label: String, value: String, tint: Color = .primary) {
        self.id = id ?? label
        self.label = label
        self.value = value
        self.tint = tint
    }
}

struct HeaderTelemetryRail: View {
    let statusTitle: String
    let statusColor: Color
    let updatedLabel: String
    let metrics: [HeaderRailMetric]
    let statusIdentifier: String?

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .center, spacing: 8) {
                leadingChips
                metricRow(limit: metrics.count)
            }

            HStack(alignment: .center, spacing: 8) {
                leadingChips
                metricRow(limit: min(metrics.count, 2))
            }
        }
    }

    private var leadingChips: some View {
        HStack(spacing: 8) {
            headerSummaryStatusPill(title: statusTitle, color: statusColor, identifier: statusIdentifier)
            headerSummaryLabel("Updated \(updatedLabel)")
        }
    }

    private func metricRow(limit: Int) -> some View {
        HStack(alignment: .center, spacing: 9) {
            ForEach(Array(metrics.prefix(limit))) { metric in
                Text("\(metric.label) \(metric.value)")
                    .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                    .foregroundStyle(metric.tint)
                    .lineLimit(1)
                    .minimumScaleFactor(0.82)
                    .monospacedDigit()
            }
        }
    }
}

struct HeaderSessionToken: Identifiable {
    let id: String
    let provider: String
    let attention: ManagedAttentionKind

    init(id: String? = nil, provider: String, attention: ManagedAttentionKind) {
        self.id = id ?? UUID().uuidString
        self.provider = provider
        self.attention = attention
    }
}

struct HeaderSessionRibbon: View {
    let statusTitle: String
    let statusColor: Color
    let updatedLabel: String
    let tokens: [HeaderSessionToken]
    let managedSummary: String
    let statusIdentifier: String?

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .center, spacing: 8) {
                leadingChips
                if !tokens.isEmpty {
                    SessionTokenRibbon(tokens: tokens)
                }
                if !managedSummary.isEmpty {
                    summaryText
                }
            }

            HStack(alignment: .center, spacing: 8) {
                leadingChips
                if !tokens.isEmpty {
                    SessionTokenRibbon(tokens: tokens)
                }
            }
        }
    }

    private var leadingChips: some View {
        HStack(spacing: 8) {
            headerSummaryStatusPill(title: statusTitle, color: statusColor, identifier: statusIdentifier)
            headerSummaryLabel("Updated \(updatedLabel)")
        }
    }

    private var summaryText: some View {
        headerSummaryLabel(managedSummary)
    }
}

private struct SessionTokenRibbon: View {
    let tokens: [HeaderSessionToken]

    var body: some View {
        HStack(spacing: 5) {
            ForEach(tokens) { token in
                ProviderGlyph(provider: token.provider, size: 14, variant: .chip)
                    .opacity(token.attention == .idle ? 0.58 : 1)
                    .overlay(alignment: .trailing) {
                        if token.attention == .needsYou || token.attention == .blocked {
                            Circle()
                                .fill(Color(red: 0.95, green: 0.70, blue: 0.20))
                                .frame(width: 4, height: 4)
                                .offset(x: 1)
                        }
                    }
            }
        }
        .padding(.vertical, 2)
    }
}

private struct MissionReadoutCell: View {
    let readout: PanelReadout

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(readout.label.uppercased())
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundStyle(Color.secondary)
                .tracking(0.55)

            Text(readout.value)
                .font(.system(size: 20, weight: .bold))
                .foregroundStyle(readout.tone)
                .monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.74)

            Text(readout.detail.uppercased())
                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                .foregroundStyle(Color.secondary)
                .tracking(0.45)
                .lineLimit(1)
                .minimumScaleFactor(0.72)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
    }
}

@MainActor
@ViewBuilder
func headerSummaryStatusPill(title: String, color: Color, identifier: String? = nil) -> some View {
    Text(title)
        .font(.system(size: 10, weight: .bold, design: .monospaced))
        .foregroundStyle(color)
        .tracking(0.4)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(
            Capsule(style: .continuous)
                .fill(color.opacity(0.16))
        )
        .lineLimit(1)
        .minimumScaleFactor(0.82)
        .applyHarnessAccessibility(identifier: identifier, label: title)
}

@MainActor
@ViewBuilder
func headerSummaryLabel(_ text: String) -> some View {
    Text(text)
        .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
        .foregroundStyle(Color.secondary)
        .monospacedDigit()
        .lineLimit(1)
        .minimumScaleFactor(0.82)
}

struct ActivityFeedEntry: Identifiable {
    let id: String
    let provider: String
    let title: String
    let age: String
    let tone: Color

    init(id: String? = nil, provider: String, title: String, age: String, tone: Color = .primary) {
        self.id = id ?? "\(provider)-\(age)-\(title)"
        self.provider = provider
        self.title = title
        self.age = age
        self.tone = tone
    }
}

struct ActivityFeed: View {
    let entries: [ActivityFeedEntry]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(entries.enumerated()), id: \.element.id) { index, entry in
                HStack(alignment: .center, spacing: 10) {
                    ProviderGlyph(provider: entry.provider, size: 16, variant: .chip)

                    Text(entry.title)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(entry.tone)
                        .lineLimit(1)
                        .minimumScaleFactor(0.84)

                    Spacer(minLength: 8)

                    Text(entry.age)
                        .font(.system(size: 12, weight: .bold, design: .monospaced))
                        .foregroundStyle(Color.primary)
                        .monospacedDigit()
                        .lineLimit(1)
                }
                .padding(.vertical, 7)

                if index < entries.count - 1 {
                    sectionDivider
                }
            }
        }
    }
}

struct UnmanagedActivityEntry: Identifiable {
    let id: String
    let provider: String
    let title: String
    let branch: String?
    let age: String
}

struct UnmanagedActivityList: View {
    let entries: [UnmanagedActivityEntry]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(entries.enumerated()), id: \.element.id) { index, entry in
                HStack(alignment: .center, spacing: 8) {
                    ProviderGlyph(provider: entry.provider, size: 16, variant: .chip)

                    Text(entry.title)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Color.primary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.84)

                    if let branch = entry.branch, !branch.isEmpty {
                        Text("/ \(branch)")
                            .font(.system(size: 11, weight: .semibold, design: .monospaced))
                            .foregroundStyle(Color.secondary)
                            .lineLimit(1)
                            .minimumScaleFactor(0.75)
                    }

                    Spacer(minLength: 8)

                    unmanagedPill

                    Text(entry.age)
                        .font(.system(size: 11, weight: .bold, design: .monospaced))
                        .foregroundStyle(Color.primary)
                        .monospacedDigit()
                        .lineLimit(1)
                }
                .padding(.vertical, 7)

                if index < entries.count - 1 {
                    sectionDivider
                }
            }
        }
    }

    /// Outline amber pill — visually distinct from the filled amber NEEDS YOU
    /// pill so users don't read "unmanaged" as "waiting on me". It's a passive
    /// "wrap this next time" badge, not an action cue.
    private var unmanagedPill: some View {
        let tint = Color(red: 0.95, green: 0.70, blue: 0.20)
        return Text("UNMANAGED")
            .font(.system(size: 9, weight: .bold, design: .monospaced))
            .foregroundStyle(tint)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .overlay(
                Capsule(style: .continuous)
                    .strokeBorder(tint.opacity(0.55), lineWidth: 1)
            )
    }
}

public enum ManagedAttentionKind: Equatable, Sendable {
    /// Managed, attached, agent is doing work. Don't interrupt.
    case working
    /// Managed, attached, waiting for the user to act (prompt, approve tool, reply).
    case needsYou
    /// Managed, attached, blocked on an external tool or approval.
    case blocked
    /// Managed, attached, sitting idle with nothing to say. No pill.
    case idle
    /// Managed but detached — bridge lost its TUI.
    case detached
    /// Managed but the bridge itself is in trouble.
    case degraded
    /// Unknown — raw state we don't have a rule for.
    case unknown(String)
}

struct ManagedSessionEntry: Identifiable {
    let id: String
    let sessionID: String?
    let provider: String
    let title: String
    let attention: ManagedAttentionKind
    let ageLabel: String
    let detail: String
    let openAction: (() -> Void)?
    let stopAction: (() -> Void)?

    init(
        id: String,
        sessionID: String?,
        provider: String,
        title: String,
        attention: ManagedAttentionKind,
        ageLabel: String,
        detail: String,
        openAction: (() -> Void)? = nil,
        stopAction: (() -> Void)? = nil
    ) {
        self.id = id
        self.sessionID = sessionID
        self.provider = provider
        self.title = title
        self.attention = attention
        self.ageLabel = ageLabel
        self.detail = detail
        self.openAction = openAction
        self.stopAction = stopAction
    }
}

struct ManagedSessionList: View {
    let entries: [ManagedSessionEntry]
    let bulkStopAction: (() -> Void)?
    let bulkStopTargetCount: Int

    init(
        entries: [ManagedSessionEntry],
        bulkStopAction: (() -> Void)? = nil,
        bulkStopTargetCount: Int = 0
    ) {
        self.entries = entries
        self.bulkStopAction = bulkStopAction
        self.bulkStopTargetCount = bulkStopTargetCount
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(entries.enumerated()), id: \.element.id) { index, entry in
                ManagedSessionRow(entry: entry)

                if index < entries.count - 1 {
                    sectionDivider
                }
            }

            if let bulkStopAction, bulkStopTargetCount > 0 {
                if !entries.isEmpty {
                    sectionDivider
                }

                BulkStopActionRow(
                    title: "Clean up sessions needing attention",
                    detail: "\(bulkStopTargetCount) sessions on this Mac",
                    tint: Color(red: 0.90, green: 0.67, blue: 0.16),
                    accessibilityIdentifier: LonghouseMenuBarAccessibilityID.Button.stopAllBackgroundManaged,
                    accessibilityLabel: "Clean up managed sessions needing attention",
                    action: bulkStopAction
                )
            }
        }
    }
}

private struct BulkStopActionRow: View {
    let title: String
    let detail: String
    let tint: Color
    let accessibilityIdentifier: String
    let accessibilityLabel: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(alignment: .center, spacing: 9) {
                Image(systemName: "xmark.circle")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(tint)
                    .frame(width: 18, height: 18)

                VStack(alignment: .leading, spacing: 1) {
                    Text(title)
                        .font(.system(size: 11.5, weight: .bold))
                        .foregroundStyle(Color.primary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.85)

                    Text(detail)
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(Color.secondary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.8)
                }

                Spacer(minLength: 8)

                Image(systemName: "chevron.right")
                    .font(.system(size: 10.5, weight: .bold))
                    .foregroundStyle(Color.secondary.opacity(0.72))
            }
            .contentShape(Rectangle())
            .padding(.vertical, 8)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier(accessibilityIdentifier)
        .accessibilityLabel(accessibilityLabel)
    }
}

private struct ManagedSessionRow: View {
    let entry: ManagedSessionEntry
    @State private var isOpenHovered = false

    var body: some View {
        HStack(alignment: .center, spacing: 6) {
            if let openAction = entry.openAction {
                Button(action: openAction) {
                    rowMainContent(isOpenable: true)
                        .padding(.vertical, 8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                        .background {
                            Rectangle()
                                .fill(Color.primary.opacity(isOpenHovered ? 0.06 : 0))
                        }
                }
                .buttonStyle(.plain)
                .accessibilityLabel(Text("Open \(entry.title) in Longhouse"))
                .onHover { hovering in
                    isOpenHovered = hovering
                }
                .onDisappear(perform: resetOpenHover)
            } else {
                rowMainContent(isOpenable: false)
                    .padding(.vertical, 8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
            }

            if let stopAction = entry.stopAction {
                inlineActionButton(
                    systemImage: "xmark.circle",
                    tint: attentionColor(entry.attention),
                    accessibilityLabel: "Stop managed session"
                ) {
                    stopAction()
                }
            }
        }
    }

    private func rowMainContent(isOpenable: Bool) -> some View {
        HStack(alignment: .center, spacing: 8) {
            ProviderGlyph(provider: entry.provider, size: 16, variant: .chip)

            VStack(alignment: .leading, spacing: 2) {
                Text(entry.title)
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(Color.primary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.78)

                if !entry.detail.isEmpty {
                    Text(entry.detail)
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(Color.secondary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.8)
                }
            }

            Spacer(minLength: 8)

            attentionPill(entry.attention)

            Text(entry.ageLabel)
                .font(.system(size: 11, weight: .bold, design: .monospaced))
                .foregroundStyle(Color.primary)
                .monospacedDigit()

            if isOpenable {
                Image(systemName: "chevron.right")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(Color.secondary.opacity(isOpenHovered ? 0.9 : 0.55))
                    .frame(width: 10, height: 16)
                    .accessibilityHidden(true)
            }
        }
        .contentShape(Rectangle())
    }

    private func resetOpenHover() {
        isOpenHovered = false
    }
}

@MainActor
@ViewBuilder
private func attentionPill(_ kind: ManagedAttentionKind, identifier: String? = nil) -> some View {
    switch kind {
    case .working:
        statePill(title: "THINKING", color: attentionColor(kind), identifier: identifier)
    case .needsYou:
        statePill(title: "NEEDS YOU", color: attentionColor(kind), identifier: identifier)
    case .blocked:
        statePill(title: "BLOCKED", color: attentionColor(kind), identifier: identifier)
    case .detached:
        statePill(title: "DETACHED", color: attentionColor(kind), identifier: identifier)
    case .degraded:
        statePill(title: "DEGRADED", color: attentionColor(kind), identifier: identifier)
    case .idle:
        EmptyView()
    case .unknown(let label):
        let trimmed = label.trimmingCharacters(in: .whitespacesAndNewlines)
        let title = trimmed.isEmpty ? "UNKNOWN" : trimmed.uppercased()
        statePill(title: title, color: attentionColor(kind), identifier: identifier)
    }
}

private func attentionColor(_ kind: ManagedAttentionKind) -> Color {
    switch kind {
    case .working:
        // Muted neutral — "don't interrupt". Deliberately not green; green is
        // reserved for overall system health at the panel header.
        return Color.secondary
    case .needsYou, .blocked:
        return Color(red: 0.95, green: 0.70, blue: 0.20)
    case .detached:
        return Color(red: 0.90, green: 0.67, blue: 0.16)
    case .degraded:
        return Color(red: 0.86, green: 0.29, blue: 0.23)
    case .idle:
        return Color.secondary
    case .unknown:
        return Color(red: 0.86, green: 0.29, blue: 0.23)
    }
}

struct BackgroundBridgeEntry: Identifiable {
    let id: String
    let sessionID: String?
    let provider: String
    let workspace: String
    let statusLabel: String
    let ageLabel: String
    let detail: String
    let stopAction: (() -> Void)?
}

struct BackgroundBridgeList: View {
    let entries: [BackgroundBridgeEntry]
    let bulkStopAction: (() -> Void)?
    let bulkStopTargetCount: Int

    init(
        entries: [BackgroundBridgeEntry],
        bulkStopAction: (() -> Void)? = nil,
        bulkStopTargetCount: Int = 0
    ) {
        self.entries = entries
        self.bulkStopAction = bulkStopAction
        self.bulkStopTargetCount = bulkStopTargetCount
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(entries.enumerated()), id: \.element.id) { index, entry in
                BackgroundBridgeRow(entry: entry)

                if index < entries.count - 1 {
                    sectionDivider
                }
            }

            if let bulkStopAction, bulkStopTargetCount > 0 {
                if !entries.isEmpty {
                    sectionDivider
                }

                BulkStopActionRow(
                    title: "Clean up detached bridges",
                    detail: "\(bulkStopTargetCount) detached bridges on this Mac",
                    tint: .red,
                    accessibilityIdentifier: LonghouseMenuBarAccessibilityID.Button.stopAllBackgroundBridges,
                    accessibilityLabel: "Clean up detached bridges",
                    action: bulkStopAction
                )
            }
        }
    }
}

private struct BackgroundBridgeRow: View {
    let entry: BackgroundBridgeEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 8) {
                ProviderGlyph(provider: entry.provider, size: 16, variant: .chip)

                Text(entry.workspace)
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(Color.primary)
                    .lineLimit(1)

                Spacer(minLength: 8)

                statePill(title: entry.statusLabel.uppercased(), color: .red)

                Text(entry.ageLabel)
                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                    .foregroundStyle(Color.primary)
                    .monospacedDigit()

                if let stopAction = entry.stopAction {
                    inlineActionButton(
                        systemImage: "xmark.circle",
                        tint: .red,
                        accessibilityLabel: "Stop background bridge"
                    ) {
                        stopAction()
                    }
                }
            }

            Text("\(HealthSnapshot.providerDisplayName(entry.provider)) bridge · \(entry.detail)")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(Color.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.vertical, 8)
    }
}

@MainActor
private func inlineActionButton(
    systemImage: String,
    tint: Color,
    accessibilityLabel: String,
    action: @escaping () -> Void
) -> some View {
    Button(action: action) {
        Image(systemName: systemImage)
            .font(.system(size: 12, weight: .semibold))
            .foregroundStyle(tint)
            .frame(width: 22, height: 22)
            .contentShape(Rectangle())
    }
    .buttonStyle(.plain)
    .accessibilityLabel(Text(accessibilityLabel))
}

struct AdaptiveTagGrid<Content: View>: View {
    let content: () -> Content

    private let columns = [
        GridItem(.adaptive(minimum: 96, maximum: 180), spacing: 6)
    ]

    init(@ViewBuilder content: @escaping () -> Content) {
        self.content = content
    }

    var body: some View {
        LazyVGrid(columns: columns, alignment: .leading, spacing: 6) {
            content()
        }
    }
}

@MainActor
func statusChip(title: String, color: Color, identifier: String? = nil) -> some View {
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

func subtleChip(title: String, tint: Color = Color.secondary) -> some View {
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

@MainActor
func statePill(title: String, color: Color, identifier: String? = nil) -> some View {
    Text(title)
        .font(.system(size: 9, weight: .bold, design: .monospaced))
        .foregroundStyle(color)
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(
            Capsule(style: .continuous)
                .fill(color.opacity(0.14))
        )
        .applyHarnessAccessibility(identifier: identifier, label: title)
}

var sectionDivider: some View {
    Rectangle()
        .fill(Color.white.opacity(0.06))
        .frame(height: 1)
}

func deckColumnTitle(_ title: String) -> some View {
    Text(title.uppercased())
        .font(.system(size: 10, weight: .bold, design: .monospaced))
        .foregroundStyle(Color.secondary)
        .tracking(0.7)
}

func statusEmblem(color: Color, systemImage: String) -> some View {
    ZStack {
        Circle()
            .fill(color.opacity(0.14))
            .frame(width: 34, height: 34)
        Image(systemName: systemImage)
            .font(.system(size: 16, weight: .semibold))
            .foregroundStyle(color)
    }
}

@MainActor
func longhouseBrandEmblem(severity: HarnessSeverity) -> some View {
    MenuBarBrandIcon.panelImage(severity: severity)
        .resizable()
        .interpolation(.high)
        .antialiased(true)
        .aspectRatio(contentMode: .fit)
        .frame(width: 30, height: 30)
        .shadow(color: severity.accentColor.opacity(0.18), radius: 8, x: 0, y: 3)
}

func providerColor(_ raw: String) -> Color {
    switch raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
    case "claude":
        return Color(red: 0xD9 / 255, green: 0x77 / 255, blue: 0x57 / 255)
    case "codex", "openai":
        return Color(red: 0xF3 / 255, green: 0xEA / 255, blue: 0xD9 / 255)
    case "opencode":
        return Color(red: 0xC9 / 255, green: 0xC4 / 255, blue: 0xC4 / 255)
    case "gemini", "antigravity":
        return Color(red: 0x4F / 255, green: 0x87 / 255, blue: 0xED / 255)
    case "cursor":
        // Official Cursor brand ink from cursor.com/brand (#14120B).
        return Color(red: 0x14 / 255, green: 0x12 / 255, blue: 0x0B / 255)
    case "zai":
        return Color(red: 0xB0 / 255, green: 0x6E / 255, blue: 0x8A / 255)
    default:
        return Color(red: 0x9A / 255, green: 0x8F / 255, blue: 0x7E / 255)
    }
}

struct ProminentActionButtonStyle: ViewModifier {
    let tint: Color

    func body(content: Content) -> some View {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *) {
            content
                .buttonStyle(.glassProminent)
                .tint(tint)
        } else {
            content
                .buttonStyle(.borderedProminent)
                .tint(tint)
        }
        #else
        content
            .buttonStyle(.borderedProminent)
            .tint(tint)
        #endif
    }
}

struct SecondaryActionButtonStyle: ViewModifier {
    func body(content: Content) -> some View {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *) {
            content.buttonStyle(.glass)
        } else {
            content.buttonStyle(.bordered)
        }
        #else
        content.buttonStyle(.bordered)
        #endif
    }
}

extension View {
    func harnessAccessibility(identifier: String, label: String) -> some View {
        accessibilityIdentifier(identifier)
            .accessibilityLabel(Text(label))
    }

    @ViewBuilder
    func applyHarnessAccessibility(identifier: String?, label: String) -> some View {
        if let identifier {
            self
                .accessibilityIdentifier(identifier)
                .accessibilityLabel(Text(label))
        } else {
            self
        }
    }
}
