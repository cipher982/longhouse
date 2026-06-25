import SwiftUI
import AppKit

/// Provider brand glyph — the real logo mark for each AI coding agent. This
/// mirrors the iOS shared ProviderGlyph surface and uses the same vector PDFs.
struct ProviderGlyph: View {
    enum Variant {
        case chip
        case bare
    }

    let provider: String?
    let size: CGFloat
    let variant: Variant

    init(provider: String?, size: CGFloat = 18, variant: Variant = .chip) {
        self.provider = provider
        self.size = size
        self.variant = variant
    }

    private var key: String {
        let raw = (provider ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        return raw == "gemini" ? "antigravity" : raw
    }

    private var assetPDF: (file: String, subdirectory: String)? {
        switch key {
        case "codex", "openai":
            return ("codex", "ProviderAssets.xcassets/ProviderCodex.imageset")
        case "claude":
            return ("claude", "ProviderAssets.xcassets/ProviderClaude.imageset")
        case "opencode":
            return ("opencode", "ProviderAssets.xcassets/ProviderOpencode.imageset")
        case "antigravity":
            return ("antigravity", "ProviderAssets.xcassets/ProviderAntigravity.imageset")
        default: return nil
        }
    }

    private var brand: Color {
        providerColor(key)
    }

    private var chipFill: Color {
        switch key {
        case "codex", "openai":
            return Color(red: 0.08, green: 0.09, blue: 0.09)
        case "opencode":
            return Color(red: 0.12, green: 0.17, blue: 0.22)
        default:
            return brand.opacity(0.16)
        }
    }

    private var chipStroke: Color {
        switch key {
        case "codex", "openai":
            return Color.white.opacity(0.32)
        case "opencode":
            return Color(red: 0.40, green: 0.74, blue: 0.92).opacity(0.45)
        default:
            return brand.opacity(0.22)
        }
    }

    private var chipCornerRadius: CGFloat {
        switch key {
        case "codex", "openai":
            return size / 2
        case "opencode":
            return max(3, size * 0.18)
        default:
            return max(4, size * 0.28)
        }
    }

    private var templateMarkColor: Color? {
        switch key {
        case "codex", "openai":
            return Color.white.opacity(0.92)
        case "opencode":
            return Color(red: 0.52, green: 0.82, blue: 0.98)
        default:
            return nil
        }
    }

    private var providerImage: NSImage? {
        guard let assetPDF,
              let url = LonghouseResourceLocator.coreBundle()?.url(
                forResource: assetPDF.file,
                withExtension: "pdf",
                subdirectory: assetPDF.subdirectory
              ) else {
            return nil
        }
        return NSImage(contentsOf: url)
    }

    @ViewBuilder
    private var mark: some View {
        if let providerImage {
            if let templateMarkColor {
                Image(nsImage: providerImage)
                    .resizable()
                    .renderingMode(.template)
                    .foregroundStyle(templateMarkColor)
                    .aspectRatio(contentMode: .fit)
            } else {
                Image(nsImage: providerImage)
                    .resizable()
                    .renderingMode(.original)
                    .aspectRatio(contentMode: .fit)
            }
        } else {
            Image(systemName: "chevron.left.forwardslash.chevron.right")
                .font(.system(size: size * 0.58, weight: .semibold))
                .foregroundStyle(Color.secondary)
        }
    }

    var body: some View {
        switch variant {
        case .bare:
            mark
                .frame(width: size, height: size)
                .accessibilityLabel(Text(HealthSnapshot.providerDisplayName(key)))
        case .chip:
            let markSize = size * 0.64
            mark
                .frame(width: markSize, height: markSize)
                .frame(width: size, height: size)
                .background(
                    RoundedRectangle(cornerRadius: chipCornerRadius, style: .continuous)
                        .fill(chipFill)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: chipCornerRadius, style: .continuous)
                        .strokeBorder(chipStroke, lineWidth: 0.5)
                )
                .accessibilityLabel(Text(HealthSnapshot.providerDisplayName(key)))
        }
    }
}
