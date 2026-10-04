import AppKit
import SwiftUI

/// Liquid Glass panel chrome shared by every panel state.
struct PanelChrome<Content: View>: View {
    let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        ZStack(alignment: .topLeading) {
            PanelMaterialBackground(cornerRadius: MenuBarPanelLayout.chromeCornerRadius)

            content
                .padding(MenuBarPanelLayout.chromePadding)
        }
        .frame(width: MenuBarPanelLayout.panelWidth, alignment: .topLeading)
        .fixedSize(horizontal: false, vertical: true)
        .clipShape(RoundedRectangle(cornerRadius: MenuBarPanelLayout.chromeCornerRadius, style: .continuous))
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

struct UnmanagedActivityEntry: Identifiable {
    let id: String
    let provider: String
    let title: String
    let branch: String?
    let age: String
}

public enum ManagedAttentionKind: Equatable, Sendable {
    /// Managed, attached, agent is doing work. Don't interrupt.
    case working
    /// Managed, attached, waiting for the user to act (prompt, approve tool, reply).
    case needsYou
    /// Managed, attached, blocked on an external tool or approval.
    case blocked
    /// Managed, attached, sitting idle with nothing to say.
    case idle
    /// Managed but detached — bridge lost its TUI.
    case detached
    /// Managed but the bridge itself is in trouble.
    case degraded
    /// Runtime Host phase truth has not arrived yet. Report once, rather than
    /// repeating a diagnostic sentence on every row.
    case phaseUnavailable
    /// Unknown — raw state we don't have a rule for.
    case unknown(String)
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

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(entries) { BackgroundBridgeRow(entry: $0) }

            if let bulkStopAction, bulkStopTargetCount > 0 {
                Button(action: bulkStopAction) {
                    Label(
                        "Stop \(bulkStopTargetCount) detached bridge\(bulkStopTargetCount == 1 ? "" : "s")",
                        systemImage: "stop.circle"
                    )
                    .font(.system(size: 11, weight: .medium))
                }
                .buttonStyle(.plain)
                .foregroundStyle(HearthPalette.fault)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.stopAllBackgroundBridges)
                .accessibilityLabel("Clean up detached bridges")
            }
        }
    }
}

private struct BackgroundBridgeRow: View {
    let entry: BackgroundBridgeEntry

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            ProviderGlyph(provider: entry.provider, size: 13, variant: .bare)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(entry.workspace)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Color.primary)
                        .lineLimit(1)
                    Text(entry.statusLabel)
                        .font(.system(size: 11))
                        .foregroundStyle(HearthPalette.fault)
                }
                Text("\(HealthSnapshot.providerDisplayName(entry.provider)) bridge · \(entry.detail)")
                    .font(.system(size: 11))
                    .foregroundStyle(Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 6)
            Text(entry.ageLabel)
                .font(.system(size: 11).monospacedDigit())
                .foregroundStyle(Color.secondary)
            if let stopAction = entry.stopAction {
                Button(action: stopAction) {
                    Image(systemName: "stop.circle")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(HearthPalette.fault)
                        .frame(width: 20, height: 20)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("Stop this background bridge")
                .accessibilityLabel(Text("Stop background bridge"))
            }
        }
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
}

extension View {
    func harnessAccessibility(identifier: String, label: String) -> some View {
        accessibilityIdentifier(identifier)
            .accessibilityLabel(Text(label))
    }
}
