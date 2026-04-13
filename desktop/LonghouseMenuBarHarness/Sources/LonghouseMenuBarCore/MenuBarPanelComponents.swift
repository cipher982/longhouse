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

struct PulseWindowModel {
    struct SignalSummary {
        let points: [Int]
        let currentValue: Int
        let minimumValue: Int
        let maximumValue: Int

        var hasVariance: Bool {
            minimumValue != maximumValue
        }
    }

    private enum BucketAggregation {
        case averageRounded
        case max
    }

    private let history: [SnapshotHistorySample]
    private let retentionMinutes: Int
    private let maxPoints: Int

    init(
        history: [SnapshotHistorySample],
        retentionMinutes: Int = 30,
        maxPoints: Int = 16
    ) {
        self.history = history
        self.retentionMinutes = retentionMinutes
        self.maxPoints = maxPoints
    }

    var hasEnoughHistory: Bool {
        history.count > 1
    }

    var coverageLabel: String {
        guard hasEnoughHistory,
              let first = history.first?.capturedAt,
              let last = history.last?.capturedAt else {
            return "Collecting"
        }
        let seconds = max(0, last.timeIntervalSince(first))
        let minutes = max(1, Int(ceil(seconds / 60.0)))
        return "Last \(min(minutes, retentionMinutes))m"
    }

    var latestActivityCount: Int {
        history.last?.sessionsRecent ?? 0
    }

    var latestQueueDepth: Int {
        guard let latest = history.last else {
            return 0
        }
        return latest.spoolPendingCount + latest.outboxCount
    }

    var activity: SignalSummary {
        makeSignalSummary(
            currentValue: latestActivityCount,
            values: bucketedValues(
                metric: { $0.sessionsRecent },
                aggregation: .averageRounded
            )
        )
    }

    var queue: SignalSummary {
        makeSignalSummary(
            currentValue: latestQueueDepth,
            values: bucketedValues(
                metric: { $0.spoolPendingCount + $0.outboxCount },
                aggregation: .max
            )
        )
    }

    var isSteadyState: Bool {
        hasEnoughHistory && !activity.hasVariance && !queue.hasVariance
    }

    var shouldChartActivity: Bool {
        hasEnoughHistory && activity.hasVariance
    }

    var shouldChartQueue: Bool {
        hasEnoughHistory && (queue.hasVariance || queue.maximumValue > 0)
    }

    var trailingLabel: String {
        guard hasEnoughHistory else {
            return "Collecting"
        }
        if isSteadyState {
            if latestQueueDepth > 0 {
                return "Steady"
            }
            if latestActivityCount > 0 {
                return "Stable"
            }
            return "Idle"
        }
        if latestQueueDepth > 0 {
            return "Queue \(latestQueueDepth)"
        }
        if latestActivityCount > 0 {
            return "\(latestActivityCount) active"
        }
        return "Live"
    }

    var steadyHeadline: String {
        let sessionSummary = latestActivityCount == 1 ? "1 recent session" : "\(latestActivityCount) recent sessions"
        let queueSummary = latestQueueDepth == 0 ? "queue idle" : "queue holding at \(latestQueueDepth)"
        return "\(sessionSummary) · \(queueSummary)"
    }

    var steadyDetail: String {
        "No meaningful variance across \(coverageLabel.lowercased())"
    }

    var activityStatusLabel: String {
        signalStatusLabel(
            currentValue: latestActivityCount,
            minimumValue: activity.minimumValue,
            maximumValue: activity.maximumValue,
            emptyLabel: "idle"
        )
    }

    var queueStatusLabel: String {
        signalStatusLabel(
            currentValue: latestQueueDepth,
            minimumValue: queue.minimumValue,
            maximumValue: queue.maximumValue,
            emptyLabel: "idle"
        )
    }

    private func makeSignalSummary(currentValue: Int, values: [Int]) -> SignalSummary {
        let resolvedValues = values.isEmpty ? [0] : values
        return SignalSummary(
            points: resolvedValues,
            currentValue: currentValue,
            minimumValue: resolvedValues.min() ?? 0,
            maximumValue: resolvedValues.max() ?? 0
        )
    }

    private func bucketedValues(
        metric: (SnapshotHistorySample) -> Int,
        aggregation: BucketAggregation
    ) -> [Int] {
        guard !history.isEmpty else {
            return []
        }
        guard history.count > maxPoints else {
            return history.map { max(0, metric($0)) }
        }

        let bucketSize = Double(history.count) / Double(maxPoints)
        return (0..<maxPoints).map { bucketIndex in
            let lowerBound = Int(floor(Double(bucketIndex) * bucketSize))
            let upperBound = min(history.count, Int(ceil(Double(bucketIndex + 1) * bucketSize)))
            let slice = history[lowerBound..<max(lowerBound + 1, upperBound)]
            let values = slice.map { max(0, metric($0)) }

            switch aggregation {
            case .averageRounded:
                let total = values.reduce(0, +)
                return Int((Double(total) / Double(max(values.count, 1))).rounded())
            case .max:
                return values.max() ?? 0
            }
        }
    }

    private func signalStatusLabel(
        currentValue: Int,
        minimumValue: Int,
        maximumValue: Int,
        emptyLabel: String
    ) -> String {
        if maximumValue == 0 {
            return emptyLabel
        }
        if minimumValue == maximumValue {
            return "steady \(currentValue)"
        }
        return "now \(currentValue) · range \(minimumValue)-\(maximumValue)"
    }
}

struct PulseWindowView: View {
    let model: PulseWindowModel

    var body: some View {
        if !model.hasEnoughHistory {
            Text("Collecting live shipping samples")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(Color.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, 8)
        } else if model.isSteadyState {
            steadyStateView
        } else {
            VStack(alignment: .leading, spacing: 10) {
                if model.shouldChartActivity {
                    PulseSignalRow(
                        title: "Sessions",
                        status: model.activityStatusLabel,
                        points: model.activity.points,
                        tint: Color(red: 0.31, green: 0.78, blue: 0.50)
                    )
                } else {
                    PulseSignalSummaryRow(title: "Sessions", status: model.activityStatusLabel)
                }

                if model.shouldChartQueue {
                    PulseSignalRow(
                        title: "Queue",
                        status: model.queueStatusLabel,
                        points: model.queue.points,
                        tint: Color(red: 0.90, green: 0.74, blue: 0.30)
                    )
                } else {
                    PulseSignalSummaryRow(title: "Queue", status: model.queueStatusLabel)
                }

                HStack {
                    Text(model.coverageLabel)
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(Color.secondary)
                    Spacer()
                    Text("Now")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(Color.secondary)
                }
            }
        }
    }

    private var steadyStateView: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                steadyMetric(title: "Sessions", value: model.latestActivityCount == 0 ? "Idle" : "\(model.latestActivityCount)")
                steadyMetric(title: "Queue", value: model.latestQueueDepth == 0 ? "Idle" : "\(model.latestQueueDepth)")
            }

            Text(model.steadyHeadline)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Color.primary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)

            Text(model.steadyDetail)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(Color.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
        }
        .padding(.vertical, 4)
    }

    @ViewBuilder
    private func steadyMetric(title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title.uppercased())
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundStyle(Color.secondary)
                .tracking(0.45)

            Text(value)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Color.primary)
                .monospacedDigit()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color.white.opacity(0.03))
        )
    }
}

private struct PulseSignalSummaryRow: View {
    let title: String
    let status: String

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            Text(title.uppercased())
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundStyle(Color.secondary)
                .tracking(0.45)

            Spacer(minLength: 8)

            Text(status)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Color.primary)
                .monospacedDigit()
        }
        .padding(.vertical, 2)
    }
}

private struct PulseSignalRow: View {
    let title: String
    let status: String
    let points: [Int]
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .center, spacing: 8) {
                Text(title.uppercased())
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundStyle(Color.secondary)
                    .tracking(0.45)

                Spacer(minLength: 8)

                Text(status)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Color.primary)
                    .monospacedDigit()
            }

            GeometryReader { geometry in
                let maxValue = max(points.max() ?? 0, 1)
                let spacing: CGFloat = 3
                let barWidth = max(6, (geometry.size.width - CGFloat(max(points.count - 1, 0)) * spacing) / CGFloat(max(points.count, 1)))

                ZStack(alignment: .bottomLeading) {
                    Rectangle()
                        .fill(Color.white.opacity(0.05))
                        .frame(height: 1)
                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)

                    HStack(alignment: .bottom, spacing: spacing) {
                        ForEach(Array(points.enumerated()), id: \.offset) { _, value in
                            let normalized = CGFloat(max(0, value)) / CGFloat(maxValue)
                            RoundedRectangle(cornerRadius: 3, style: .continuous)
                                .fill(tint.opacity(value == 0 ? 0.22 : 0.92))
                                .frame(width: barWidth, height: max(value == 0 ? 2 : 6, normalized * geometry.size.height))
                        }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomLeading)
                }
            }
            .frame(height: 18)
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
