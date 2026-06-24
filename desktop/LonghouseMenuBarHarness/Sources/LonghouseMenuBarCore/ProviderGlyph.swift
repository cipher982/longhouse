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
            Image(nsImage: providerImage)
                .resizable()
                .renderingMode(.original)
                .aspectRatio(contentMode: .fit)
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
                    RoundedRectangle(cornerRadius: max(4, size * 0.28), style: .continuous)
                        .fill(brand.opacity(0.16))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: max(4, size * 0.28), style: .continuous)
                        .strokeBorder(brand.opacity(0.22), lineWidth: 0.5)
                )
                .accessibilityLabel(Text(HealthSnapshot.providerDisplayName(key)))
        }
    }
}
