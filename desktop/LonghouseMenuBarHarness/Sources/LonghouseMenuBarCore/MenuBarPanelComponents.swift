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
        if #available(macOS 26.0, *) {
            let glass = NSGlassEffectView()
            glass.cornerRadius = cornerRadius
            return glass
        } else {
            let view = NSVisualEffectView()
            view.material = .popover
            view.blendingMode = .behindWindow
            view.state = .active
            return view
        }
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        if #available(macOS 26.0, *) {
            (nsView as? NSGlassEffectView)?.cornerRadius = cornerRadius
        }
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
        .background(
            RoundedRectangle(cornerRadius: MenuBarPanelLayout.sectionCornerRadius, style: .continuous)
                .fill(Color.primary.opacity(0.04))
        )
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
                    Circle()
                        .fill(providerColor(entry.provider))
                        .frame(width: 7, height: 7)

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

struct ProviderComparisonRows: View {
    let entries: [(provider: String, count: Int)]
    let totalCount: Int

    var body: some View {
        let baseline = max(entries.map(\.count).max() ?? 0, 1)

        if entries.isEmpty {
            Text("No tracked sessions today.")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Color.secondary)
        } else {
            VStack(alignment: .leading, spacing: 9) {
                ForEach(Array(entries.enumerated()), id: \.offset) { index, entry in
                    ProviderComparisonRow(
                        provider: entry.provider,
                        count: entry.count,
                        totalCount: totalCount,
                        baselineCount: baseline
                    )

                    if index < entries.count - 1 {
                        sectionDivider
                    }
                }
            }
        }
    }
}

private struct ProviderComparisonRow: View {
    let provider: String
    let count: Int
    let totalCount: Int
    let baselineCount: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .center, spacing: 8) {
                HStack(alignment: .center, spacing: 7) {
                    Circle()
                        .fill(providerColor(provider))
                        .frame(width: 7, height: 7)

                    Text(HealthSnapshot.providerDisplayName(provider))
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(Color.primary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.84)
                }

                Spacer(minLength: 8)

                Text("\(count)")
                    .font(.system(size: 12, weight: .bold, design: .monospaced))
                    .foregroundStyle(Color.primary)
                    .lineLimit(1)
                    .monospacedDigit()

                Text(shareLabel)
                    .font(.system(size: 10, weight: .semibold, design: .monospaced))
                    .foregroundStyle(Color.secondary)
                    .lineLimit(1)
                    .monospacedDigit()
            }

            GeometryReader { geometry in
                ZStack(alignment: .leading) {
                    Capsule(style: .continuous)
                        .fill(Color.white.opacity(0.07))

                    Capsule(style: .continuous)
                        .fill(providerColor(provider).opacity(0.85))
                        .frame(width: max(14, geometry.size.width * CGFloat(count) / CGFloat(max(baselineCount, 1))))
                }
            }
            .frame(height: 6)
        }
    }

    private var shareLabel: String {
        guard totalCount > 0 else {
            return "0%"
        }
        let percent = Int((Double(count) / Double(totalCount) * 100).rounded())
        return "\(percent)%"
    }
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

func providerColor(_ raw: String) -> Color {
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

extension View {
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
