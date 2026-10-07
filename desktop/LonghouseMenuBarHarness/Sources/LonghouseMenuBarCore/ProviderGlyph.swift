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
        case "codex", "openai":
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
        } else if key == "omp" {
            // Official mark (omp.sh/favicon.svg), same paths as the web glyph.
            OMPMarkShape()
                .fill(LinearGradient(
                    colors: [
                        Color(red: 0xED / 255, green: 0x4A / 255, blue: 0xBF / 255),
                        Color(red: 0x9B / 255, green: 0x4D / 255, blue: 0xFF / 255),
                        Color(red: 0x5A / 255, green: 0xD8 / 255, blue: 0xE6 / 255),
                    ],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                ))
                .aspectRatio(1, contentMode: .fit)
        } else if key == "pi" {
            PiMark()
                .aspectRatio(1, contentMode: .fit)
        } else if key == "zai" {
            ZAIMark()
                .aspectRatio(1, contentMode: .fit)
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

/// OMP's π mark: `M14 16h36v8H40v32h-8V24h-6v22h-8V24h-4z` in a 64 box.
private struct OMPMarkShape: Shape {
    func path(in rect: CGRect) -> Path {
        let s = min(rect.width, rect.height) / 64
        let points: [(CGFloat, CGFloat)] = [
            (14, 16), (50, 16), (50, 24), (40, 24), (40, 56), (32, 56),
            (32, 24), (26, 24), (26, 46), (18, 46), (18, 24), (14, 24),
        ]
        var path = Path()
        path.addLines(points.map { CGPoint(x: rect.minX + $0.0 * s, y: rect.minY + $0.1 * s) })
        path.closeSubpath()
        return path
    }
}

/// Pi's three-block mark (pi.dev), from the web glyph's 469.43-unit box.
private struct PiMark: View {
    var body: some View {
        Canvas { context, size in
            let s = min(size.width, size.height) / 469.43
            func block(_ points: [(CGFloat, CGFloat)], _ color: Color) {
                var path = Path()
                path.addLines(points.map { CGPoint(x: ($0.0 - 165.29) * s, y: ($0.1 - 165.29) * s) })
                path.closeSubpath()
                context.fill(path, with: .color(color))
            }
            block([(165.29, 165.29), (517.36, 165.29), (517.36, 400), (400, 400), (400, 282.65), (165.29, 282.65)],
                  Color(red: 0xF0 / 255, green: 0x90 / 255, blue: 0x82 / 255))
            block([(165.29, 282.65), (282.65, 282.65), (282.65, 400), (400, 400), (400, 517.36), (282.65, 517.36), (282.65, 634.72), (165.29, 634.72)],
                  Color(red: 0x4D / 255, green: 0x9A / 255, blue: 0xBF / 255))
            block([(517.36, 400), (634.72, 400), (634.72, 634.72), (517.36, 634.72)],
                  Color(red: 0xF1 / 255, green: 0xBE / 255, blue: 0x58 / 255))
        }
    }
}

/// Z.ai's mark: a Z with a spark, in a 24-unit box (same geometry as iOS).
private struct ZAIMark: View {
    var body: some View {
        Canvas { context, size in
            let s = min(size.width, size.height) / 24
            let ink = Color(red: 0.690196, green: 0.431373, blue: 0.541176)
            func polygon(_ points: [(CGFloat, CGFloat)]) -> Path {
                var path = Path()
                path.addLines(points.map { CGPoint(x: $0.0 * s, y: $0.1 * s) })
                path.closeSubpath()
                return path
            }
            context.fill(
                polygon([(4, 5), (20, 5), (20, 8.1), (9, 16), (20, 16), (20, 19), (4, 19), (4, 15.9), (15, 8), (4, 8)]),
                with: .color(ink)
            )
            let (cx, cy): (CGFloat, CGFloat) = (18.2, 4.2)
            context.fill(
                polygon([
                    (cx, cy - 2.2), (cx + 0.65, cy - 0.65), (cx + 2.2, cy), (cx + 0.65, cy + 0.65),
                    (cx, cy + 2.2), (cx - 0.65, cy + 0.65), (cx - 2.2, cy), (cx - 0.65, cy - 0.65),
                ]),
                with: .color(ink)
            )
        }
    }
}
