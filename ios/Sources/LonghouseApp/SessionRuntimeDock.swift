import SwiftUI

/// The status row of the control card: activity strip, headline, counting
/// elapsed time, and a mono tail line with the newest live text. Colour is
/// signal — the strip and headline take the session tone, everything else is
/// monochrome. The capability chip appears only when control is *not* live:
/// an enabled composer is the proof of the healthy case, so naming it there
/// was noise.
struct SessionRuntimeDock: View {
    let detail: SessionDetail
    @ObservedObject var activity: ActivityPulseStore

    @Environment(\.dynamicTypeSize) private var typeSize
    // A streaming provider refreshes the primary label's observed_at with
    // every provisional delta. Anchor the counter on the earliest observation
    // for the current label + tool so it counts up instead of resetting.
    @State private var elapsedAnchor: ElapsedAnchor?

    private struct ElapsedAnchor: Equatable {
        let key: String
        let start: Date
    }

    var body: some View {
        Group {
            if detail.canDraftBeforeSendReady {
                launchSetupLine
            } else {
                standardLines
            }
        }
        .padding(.horizontal, 4)
        .onAppear { reanchorElapsed() }
        .onChange(of: detail.activityStartedAt) { _, _ in reanchorElapsed() }
        .onChange(of: elapsedAnchorKey) { _, _ in reanchorElapsed() }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(accessibilityLabel)
    }

    private var style: RuntimeChromeStyle { RuntimeChromeStyle(detail: detail) }

    private var elapsedAnchorKey: String {
        [detail.stateFacts.primary?.key ?? "", detail.stateFacts.activityTool ?? "", detail.stateFacts.activityState]
            .joined(separator: ":")
    }

    private func reanchorElapsed() {
        guard let observed = detail.activityStartedAt else {
            if elapsedAnchor?.key != elapsedAnchorKey { elapsedAnchor = nil }
            return
        }
        if let current = elapsedAnchor, current.key == elapsedAnchorKey {
            if observed < current.start {
                elapsedAnchor = ElapsedAnchor(key: current.key, start: observed)
            }
        } else {
            elapsedAnchor = ElapsedAnchor(key: elapsedAnchorKey, start: observed)
        }
    }

    private var elapsedStart: Date? {
        if let anchor = elapsedAnchor, anchor.key == elapsedAnchorKey {
            return anchor.start
        }
        return detail.activityStartedAt
    }
    private var tone: Color { style.dot.color }
    private var isExecuting: Bool { detail.isSessionExecuting }

    private var standardLines: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                ActivityStrip(store: activity, tone: tone, isLive: isExecuting)
                Text(detail.runtimeHeadline)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(headlineColor)
                    .lineLimit(1)
                elapsed
                if let runtimeDetail = detail.runtimeDetail, !typeSize.isAccessibilitySize {
                    Text(runtimeDetail)
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
                Spacer(minLength: 8)
                capabilityChip
            }
            if let tail = detail.runtimeTailLine, !typeSize.isAccessibilitySize {
                Text(tail)
                    .font(.caption.monospaced())
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .truncationMode(.head)
                    .padding(.leading, ActivityStrip.size.width + 8)
                    .accessibilityIdentifier("session-runtime-tail")
            }
        }
    }

    private var launchSetupLine: some View {
        HStack(spacing: 8) {
            ActivityStrip(store: activity, tone: RuntimeSignal.live.color, isLive: true)
            Text(detail.launchSetupStatusLabel)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.primary)
                .lineLimit(1)
            Spacer(minLength: 0)
        }
    }

    private var headlineColor: Color {
        switch style.dot {
        case .attention: return TranscriptPalette.attention
        case .live: return .primary
        case .idle, .dormant: return .secondary
        }
    }

    // Executing: a precise count that ticks once a second — the cheapest honest
    // "still alive" there is. Otherwise a coarse age, and no ticking.
    @ViewBuilder
    private var elapsed: some View {
        if let start = elapsedStart {
            let evidenceLive = detail.stateFacts.activityEvidenceIsLive()
            let ticking = isExecuting && evidenceLive
            if ticking {
                SwiftUI.TimelineView(.periodic(from: .now, by: 1)) { context in
                    elapsedText(RuntimeElapsed.label(from: start, to: context.date, precise: true))
                }
            } else {
                let end = (isExecuting ? detail.stateFacts.activityValidUntil.flatMap(LonghouseDateParser.parse) : nil) ?? Date()
                elapsedText(RuntimeElapsed.label(from: start, to: end, precise: isExecuting))
            }
        }
    }

    private func elapsedText(_ label: String) -> some View {
        Text(label)
            .font(.subheadline)
            .monospacedDigit()
            .foregroundStyle(.tertiary)
            .lineLimit(1)
            .accessibilityIdentifier("session-runtime-elapsed")
    }

    // Absent while control is live. The server declines to emit a label when
    // the primary already carries the story; the client declines to repeat
    // "Live control" beside a composer that is plainly enabled.
    @ViewBuilder
    private var capabilityChip: some View {
        if style.capability != .live, let label = detail.runtimeCapabilityLabel {
            Text(label)
                .font(.caption2.weight(.medium))
                .lineLimit(1)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .foregroundStyle(style.capability == .warning ? TranscriptPalette.attention : Color.secondary)
                .background(
                    Capsule(style: .continuous).fill(
                        style.capability == .warning
                            ? TranscriptPalette.attention.opacity(0.14)
                            : Color(.quaternarySystemFill)
                    )
                )
                .accessibilityIdentifier("session-runtime-capability")
        }
    }

    private var accessibilityLabel: String {
        if detail.canDraftBeforeSendReady {
            return detail.launchSetupStatusLabel
        }
        var parts = [detail.runtimeHeadline]
        if let start = elapsedStart {
            parts.append(RuntimeElapsed.label(from: start, to: Date(), precise: isExecuting))
        }
        if let detailLabel = detail.runtimeDetail { parts.append(detailLabel) }
        if style.capability != .live, let label = detail.runtimeCapabilityLabel { parts.append(label) }
        if let tail = detail.runtimeTailLine { parts.append(tail) }
        return parts.joined(separator: ", ")
    }
}
