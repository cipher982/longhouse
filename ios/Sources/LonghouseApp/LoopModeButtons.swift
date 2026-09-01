import SwiftUI

struct LoopModeButtons: View {
    let currentMode: SessionLoopMode
    let disabled: Bool
    let onChange: (SessionLoopMode) -> Void

    var body: some View {
        Menu {
            Button { onChange(.assist) } label: {
                Label("Assist", systemImage: "wand.and.stars")
            }
            Button { onChange(.autopilot) } label: {
                Label("Autopilot", systemImage: "bolt.circle")
            }
            Divider()
            Button { onChange(.manual) } label: {
                Label("Off", systemImage: "pause.circle")
            }
        } label: {
            HStack(spacing: 3) {
                Image(systemName: modeIcon)
                    .font(.caption2.weight(.semibold))
                if !typeSize.isAccessibilitySize {
                    Text(modeLabel)
                        .font(.caption2.weight(.medium))
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                }
            }
            .lineLimit(1)
            .fixedSize(horizontal: true, vertical: false)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(Color(.quaternarySystemFill), in: Capsule())
        }
        .disabled(disabled)
        .accessibilityLabel("Loop mode: \(modeLabel)")
    }

    @Environment(\.dynamicTypeSize) private var typeSize

    private var modeLabel: String {
        switch currentMode {
        case .assist: return "Assist"
        case .autopilot: return "Auto"
        case .manual: return "Off"
        }
    }

    private var modeIcon: String {
        switch currentMode {
        case .assist: return "wand.and.stars"
        case .autopilot: return "bolt"
        case .manual: return "pause"
        }
    }
}
