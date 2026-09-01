import SwiftUI

struct SessionRuntimeDock: View {
    let detail: SessionDetail

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        // One quiet monochrome status line. The state dot is the only color;
        // headline/detail/capability are a flat type hierarchy. No background or
        // divider — the fused control card owns the surface.
        Group {
            if detail.canDraftBeforeSendReady {
                launchSetupLine
            } else {
                standardLine
            }
        }
        .padding(.horizontal, 4)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(accessibilityLabel)
    }

    private var style: RuntimeChromeStyle { RuntimeChromeStyle(detail: detail) }

    private var standardLine: some View {
        HStack(spacing: 7) {
            indicator
            Text(detail.runtimeHeadline)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.primary)
                .lineLimit(1)
            if let runtimeDetail = detail.runtimeDetail, !typeSize.isAccessibilitySize {
                Text("·").foregroundStyle(.tertiary)
                Text(runtimeDetail)
                    .font(.subheadline)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            capabilityPill
        }
    }

    private var launchSetupLine: some View {
        HStack(spacing: 7) {
            indicator
            Text(detail.launchSetupStatusLabel)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.primary)
                .lineLimit(1)
            Spacer(minLength: 0)
        }
    }

    // State dot — color is the signal; motion (breathing ring) marks "live".
    @ViewBuilder
    private var indicator: some View {
        let toneColor = style.dot.color
        ZStack {
            if detail.isSessionExecuting && !reduceMotion {
                Circle().stroke(toneColor.opacity(0.35), lineWidth: 1.5)
                    .frame(width: 13, height: 13)
            }
            Circle().fill(toneColor).frame(width: 7, height: 7)
        }
        .frame(width: 14, height: 14)
    }

    // Capability as monochrome text with a small live-dot — never colored words.
    // Absent when the contract emitted no access label: the primary label
    // already carries the whole story there, and a fabricated chip beside it
    // claims something the server declined to claim.
    @ViewBuilder
    private var capabilityPill: some View {
        if let capabilityLabel {
            HStack(spacing: 4) {
                if style.capability.showsLiveDot {
                    Circle().fill(TranscriptPalette.live).frame(width: 5, height: 5)
                }
                Text(capabilityLabel)
                    .font(.caption2.weight(.medium))
                    .lineLimit(1)
            }
            .foregroundStyle(style.capability.color)
        }
    }

    private var capabilityLabel: String? {
        guard let label = detail.runtimeCapabilityLabel else { return nil }
        let livePrefix = "Live on "
        if label.range(of: livePrefix, options: [.anchored, .caseInsensitive]) != nil {
            let hostStart = label.index(label.startIndex, offsetBy: livePrefix.count)
            let host = label[hostStart...].trimmingCharacters(in: .whitespacesAndNewlines)
            if !host.isEmpty {
                return host
            }
        }
        return label
    }

    private var accessibilityLabel: String {
        if detail.canDraftBeforeSendReady {
            return detail.launchSetupStatusLabel
        }
        return [detail.runtimeHeadline, detail.runtimeDetail, capabilityLabel]
            .compactMap { $0 }
            .joined(separator: ", ")
    }
}
