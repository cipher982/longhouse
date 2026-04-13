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

struct PulseChart: View {
    let history: [SnapshotHistorySample]

    var body: some View {
        GeometryReader { geometry in
            let samples = reducedSamples(from: history, maxPoints: 24)
            let maxValue = max(samples.map(activityValue(for:)).max() ?? 0, 1)
            let spacing: CGFloat = 3
            let barWidth = max(4, (geometry.size.width - CGFloat(max(samples.count - 1, 0)) * spacing) / CGFloat(max(samples.count, 1)))

            ZStack(alignment: .bottomLeading) {
                VStack(spacing: geometry.size.height / 3) {
                    Rectangle().fill(Color.white.opacity(0.04)).frame(height: 1)
                    Rectangle().fill(Color.white.opacity(0.04)).frame(height: 1)
                    Rectangle().fill(Color.white.opacity(0.04)).frame(height: 1)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)

                HStack(alignment: .bottom, spacing: spacing) {
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
            return Color(red: 0.31, green: 0.78, blue: 0.50)
        case .yellow:
            return Color(red: 0.90, green: 0.74, blue: 0.30)
        case .red:
            return Color(red: 0.89, green: 0.34, blue: 0.30)
        case .gray:
            return Color.secondary
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
