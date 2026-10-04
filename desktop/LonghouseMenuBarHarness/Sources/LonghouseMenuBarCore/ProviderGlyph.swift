import SwiftUI
import AppKit

/// Provider brand glyph — the real logo mark for each AI coding agent. This
/// mirrors the iOS shared ProviderGlyph surface and uses the same vector PDFs.
/// Colors and rendering rules are driven by config/provider-brands.json
/// via the generated ProviderBrands enum.
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
        ProviderBrands.canonicalKey(provider) ?? ""
    }

    private var assetPDF: (file: String, subdirectory: String)? {
        switch key {
        case "codex":
            return ("codex", "ProviderAssets.xcassets/ProviderCodex.imageset")
        case "claude":
            return ("claude", "ProviderAssets.xcassets/ProviderClaude.imageset")
        case "opencode":
            return ("opencode", "ProviderAssets.xcassets/ProviderOpencode.imageset")
        case "antigravity":
            return ("antigravity", "ProviderAssets.xcassets/ProviderAntigravity.imageset")
        case "cursor":
            return ("cursor", "ProviderAssets.xcassets/ProviderCursor.imageset")
        default: return nil
        }
    }

    private var config: ProviderBrandConfig {
        ProviderBrands.lookup(provider)
    }

    private var chipFill: Color {
        switch config.chipFillType {
        case "solid":
            return config.chipFillColor ?? config.brand.opacity(config.chipFillAlpha ?? 0.16)
        case "brand_alpha":
            return config.brand.opacity(config.chipFillAlpha ?? 0.16)
        default:
            return config.brand.opacity(0.16)
        }
    }

    private var chipStroke: Color {
        switch config.chipStrokeType {
        case "solid":
            return config.chipStrokeColor ?? config.brand.opacity(config.chipStrokeAlpha ?? 0.22)
        case "brand_alpha":
            return config.brand.opacity(config.chipStrokeAlpha ?? 0.22)
        default:
            return config.brand.opacity(0.22)
        }
    }

    private var chipCornerRadius: CGFloat {
        max(3, size * config.cornerRadiusFactor)
    }

    private var templateMarkColor: Color? {
        guard config.glyphStyle == "template" else { return nil }
        return config.markColor
    }

    @MainActor private static var imageCache: [String: NSImage] = [:]

    /// The raw PDF when the bundle carries the catalog as files, else the
    /// compiled catalog (`Assets.car`), which newer SwiftPM builds produce.
    /// Loaded once per provider, not on every body evaluation.
    private var providerImage: NSImage? {
        guard let assetPDF else { return nil }
        if let cached = Self.imageCache[assetPDF.file] { return cached }
        guard let bundle = LonghouseResourceLocator.coreBundle() else { return nil }
        let image: NSImage?
        if let url = bundle.url(forResource: assetPDF.file, withExtension: "pdf", subdirectory: assetPDF.subdirectory) {
            image = NSImage(contentsOf: url)
        } else {
            let imageSet = (assetPDF.subdirectory as NSString).lastPathComponent
            image = bundle.image(forResource: (imageSet as NSString).deletingPathExtension)
        }
        if let image { Self.imageCache[assetPDF.file] = image }
        return image
    }

    @ViewBuilder
    private var mark: some View {
        if let providerImage {
            if let templateMarkColor {
                // A bare mark has no chip behind it; a template mark tinted for
                // the dark chip vanishes on light glass, so it follows the text.
                Image(nsImage: providerImage)
                    .resizable()
                    .renderingMode(.template)
                    .foregroundStyle(variant == .bare ? AnyShapeStyle(Color.primary.opacity(0.8)) : AnyShapeStyle(templateMarkColor))
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
