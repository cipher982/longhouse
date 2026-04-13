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

private let panelCornerRadius: CGFloat = 20
private let sectionCornerRadius: CGFloat = 14

struct PanelChrome<Content: View>: View {
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
                colors: [accent.opacity(0.05), Color.clear],
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
                .strokeBorder(Color.white.opacity(0.09), lineWidth: 1)
        )
        .overlay(alignment: .top) {
            Capsule(style: .continuous)
                .fill(accent.opacity(0.64))
                .frame(height: 3)
                .padding(.horizontal, 16)
                .padding(.top, 12)
        }
        .shadow(color: Color.black.opacity(0.18), radius: 12, x: 0, y: 8)
    }
}

struct PanelMaterialBackground: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .popover
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {}
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
        VStack(alignment: .leading, spacing: 10) {
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
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: sectionCornerRadius, style: .continuous)
                .fill(Color.white.opacity(0.035))
        )
        .overlay(
            RoundedRectangle(cornerRadius: sectionCornerRadius, style: .continuous)
                .stroke(Color.white.opacity(0.07), lineWidth: 1)
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
        .padding(.horizontal, 4)
        .padding(.vertical, 4)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(Color.white.opacity(0.035))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(Color.white.opacity(0.07), lineWidth: 1)
        )
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
                .font(.system(size: 18, weight: .bold))
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

struct ProviderMixBar: View {
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
                    }
                }
                .padding(1)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(height: 14)
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

struct FlightStripSegment: Identifiable {
    let id: String
    let label: String
    let value: String
    let tone: Color

    init(id: String? = nil, label: String, value: String, tone: Color = .primary) {
        self.id = id ?? label
        self.label = label
        self.value = value
        self.tone = tone
    }
}

struct FlightStrip: View {
    let segments: [FlightStripSegment]
    let accent: Color

    var body: some View {
        HStack(alignment: .center, spacing: 0) {
            ForEach(Array(segments.enumerated()), id: \.element.id) { index, segment in
                VStack(alignment: .leading, spacing: 2) {
                    Text(segment.label.uppercased())
                        .font(.system(size: 8, weight: .bold, design: .monospaced))
                        .foregroundStyle(Color.secondary)
                        .tracking(0.5)
                    Text(segment.value)
                        .font(.system(size: 12, weight: .semibold, design: .monospaced))
                        .foregroundStyle(segment.tone)
                        .lineLimit(1)
                        .minimumScaleFactor(0.74)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 10)
                .padding(.vertical, 8)

                if index < segments.count - 1 {
                    Rectangle()
                        .fill(Color.white.opacity(0.07))
                        .frame(width: 1)
                        .padding(.vertical, 8)
                }
            }
        }
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(accent.opacity(0.08))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(Color.white.opacity(0.07), lineWidth: 1)
        )
    }
}

struct SignalCell: Identifiable {
    let id: String
    let label: String
    let value: String
    let detail: String?
    let tone: Color

    init(id: String? = nil, label: String, value: String, detail: String? = nil, tone: Color = .primary) {
        self.id = id ?? label
        self.label = label
        self.value = value
        self.detail = detail
        self.tone = tone
    }
}

struct SignalGrid: View {
    let signals: [SignalCell]
    let columns: Int

    var body: some View {
        LazyVGrid(columns: gridColumns, alignment: .leading, spacing: 8) {
            ForEach(signals) { signal in
                signalCell(signal)
            }
        }
    }

    private var gridColumns: [GridItem] {
        Array(repeating: GridItem(.flexible(minimum: 0, maximum: .infinity), spacing: 8), count: max(columns, 1))
    }

    @ViewBuilder
    private func signalCell(_ signal: SignalCell) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(signal.label.uppercased())
                .font(.system(size: 8, weight: .bold, design: .monospaced))
                .foregroundStyle(Color.secondary)
                .tracking(0.45)

            Text(signal.value)
                .font(.system(size: 14, weight: .bold, design: .monospaced))
                .foregroundStyle(signal.tone)
                .monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.72)

            if let detail = signal.detail {
                Text(detail.uppercased())
                    .font(.system(size: 8, weight: .semibold, design: .monospaced))
                    .foregroundStyle(Color.secondary)
                    .tracking(0.4)
                    .lineLimit(1)
                    .minimumScaleFactor(0.72)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
        .padding(.horizontal, 7)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color.white.opacity(0.03))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(Color.white.opacity(0.05), lineWidth: 1)
        )
    }
}

struct ProviderMixDeck: View {
    let title: String
    let summary: String
    let entries: [(provider: String, count: Int)]

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .center, spacing: 8) {
                Text(title.uppercased())
                    .font(.system(size: 8, weight: .bold, design: .monospaced))
                    .foregroundStyle(Color.secondary)
                    .tracking(0.45)

                Spacer(minLength: 8)

                Text(summary)
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Color.primary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.76)
                    .monospacedDigit()
            }

            ProviderMixBar(entries: entries)
        }
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
