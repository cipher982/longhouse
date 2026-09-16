import SwiftUI

private enum ProviderGlyphPalette {
    static let piRose = Color(red: 0.941176, green: 0.564706, blue: 0.509804)
    static let piBlue = Color(red: 0.301961, green: 0.603922, blue: 0.74902)
    static let piGold = Color(red: 0.945098, green: 0.745098, blue: 0.345098)
    static let ompPink = Color(red: 0.941176, green: 0.266667, blue: 0.780392)
    static let ompBlue = Color(red: 0.431373, green: 0.607843, blue: 1)
    static let ompOrange = Color(red: 0.976471, green: 0.45098, blue: 0.0862745)
}

/// Pi's official three-colour mark, sourced from pi.dev/logo-auto.svg.
private struct PiProviderMark: View {
    let monochrome: Bool

    var body: some View {
        Canvas { context, size in
            let scale = min(size.width, size.height) / 800
            let origin = CGPoint(
                x: (size.width - 800 * scale) / 2,
                y: (size.height - 800 * scale) / 2
            )
            func rect(_ x: CGFloat, _ y: CGFloat, _ width: CGFloat, _ height: CGFloat) -> Path {
                Path(CGRect(
                    x: origin.x + x * scale,
                    y: origin.y + y * scale,
                    width: width * scale,
                    height: height * scale
                ))
            }

            context.fill(
                rect(165.29, 165.29, 352.07, 234.71),
                with: .color(monochrome ? .white : ProviderGlyphPalette.piRose)
            )
            context.fill(
                rect(165.29, 282.65, 234.71, 352.07),
                with: .color(monochrome ? .white : ProviderGlyphPalette.piBlue)
            )
            context.fill(
                rect(517.36, 400, 117.36, 234.72),
                with: .color(monochrome ? .white : ProviderGlyphPalette.piGold)
            )
        }
    }
}

/// OMP's official Pi-plus-plugin mark, sourced from omp.sh/assets/icon.svg.
private struct OMPProviderMark: View {
    let monochrome: Bool

    var body: some View {
        Canvas { context, size in
            let scale = min(size.width / 120, size.height / 90)
            let origin = CGPoint(
                x: (size.width - 120 * scale) / 2,
                y: (size.height - 90 * scale) / 2
            )
            func roundedRect(
                _ x: CGFloat,
                _ y: CGFloat,
                _ width: CGFloat,
                _ height: CGFloat,
                _ radius: CGFloat
            ) -> Path {
                Path(
                    roundedRect: CGRect(
                        x: origin.x + x * scale,
                        y: origin.y + y * scale,
                        width: width * scale,
                        height: height * scale
                    ),
                    cornerRadius: radius * scale
                )
            }

            let gradient = Gradient(colors: [ProviderGlyphPalette.ompPink, ProviderGlyphPalette.ompBlue])
            let piShading: GraphicsContext.Shading = monochrome
                ? .color(.white)
                : .linearGradient(
                    gradient,
                    startPoint: CGPoint(x: origin.x + 10 * scale, y: origin.y + 8 * scale),
                    endPoint: CGPoint(x: origin.x + 110 * scale, y: origin.y + 82 * scale)
                )
            context.fill(roundedRect(10, 8, 100, 12, 2), with: piShading)
            context.fill(roundedRect(25, 20, 12, 62, 2), with: piShading)
            context.fill(roundedRect(75, 20, 12, 45, 2), with: piShading)
            context.fill(
                roundedRect(71, 55, 20, 16, 3),
                with: .color(monochrome ? .white : ProviderGlyphPalette.ompOrange)
            )
            context.fill(
                roundedRect(76, 59, 3, 8, 1),
                with: .color(monochrome ? Color.black : Color(red: 0.05098, green: 0.05098, blue: 0.05098))
            )
            context.fill(
                roundedRect(82, 59, 3, 8, 1),
                with: .color(monochrome ? Color.black : Color(red: 0.05098, green: 0.05098, blue: 0.05098))
            )
            context.fill(
                Path(ellipseIn: CGRect(
                    x: origin.x + 16 * scale,
                    y: origin.y + 12 * scale,
                    width: 4 * scale,
                    height: 4 * scale
                )),
                with: .color(monochrome ? .white : ProviderGlyphPalette.ompOrange)
            )
            context.fill(
                Path(ellipseIn: CGRect(
                    x: origin.x + 100 * scale,
                    y: origin.y + 12 * scale,
                    width: 4 * scale,
                    height: 4 * scale
                )),
                with: .color(monochrome ? .white : ProviderGlyphPalette.ompOrange)
            )
        }
    }
}

/// Z.ai's archive identity mark: a compact geometric Z and spark.
private struct ZAIProviderMark: View {
    var body: some View {
        Canvas { context, size in
            let scale = min(size.width, size.height) / 24
            let origin = CGPoint(
                x: (size.width - 24 * scale) / 2,
                y: (size.height - 24 * scale) / 2
            )
            var z = Path()
            z.move(to: CGPoint(x: origin.x + 4 * scale, y: origin.y + 5 * scale))
            z.addLine(to: CGPoint(x: origin.x + 20 * scale, y: origin.y + 5 * scale))
            z.addLine(to: CGPoint(x: origin.x + 20 * scale, y: origin.y + 8.1 * scale))
            z.addLine(to: CGPoint(x: origin.x + 9 * scale, y: origin.y + 16 * scale))
            z.addLine(to: CGPoint(x: origin.x + 20 * scale, y: origin.y + 16 * scale))
            z.addLine(to: CGPoint(x: origin.x + 20 * scale, y: origin.y + 19 * scale))
            z.addLine(to: CGPoint(x: origin.x + 4 * scale, y: origin.y + 19 * scale))
            z.addLine(to: CGPoint(x: origin.x + 4 * scale, y: origin.y + 15.9 * scale))
            z.addLine(to: CGPoint(x: origin.x + 15 * scale, y: origin.y + 8 * scale))
            z.addLine(to: CGPoint(x: origin.x + 4 * scale, y: origin.y + 8 * scale))
            z.closeSubpath()
            context.fill(z, with: .color(Color(red: 0.690196, green: 0.431373, blue: 0.541176)))

            let center = CGPoint(x: origin.x + 18.2 * scale, y: origin.y + 4.2 * scale)
            var spark = Path()
            spark.move(to: CGPoint(x: center.x, y: center.y - 2.2 * scale))
            spark.addLine(to: CGPoint(x: center.x + 0.65 * scale, y: center.y - 0.65 * scale))
            spark.addLine(to: CGPoint(x: center.x + 2.2 * scale, y: center.y))
            spark.addLine(to: CGPoint(x: center.x + 0.65 * scale, y: center.y + 0.65 * scale))
            spark.addLine(to: CGPoint(x: center.x, y: center.y + 2.2 * scale))
            spark.addLine(to: CGPoint(x: center.x - 0.65 * scale, y: center.y + 0.65 * scale))
            spark.addLine(to: CGPoint(x: center.x - 2.2 * scale, y: center.y))
            spark.addLine(to: CGPoint(x: center.x - 0.65 * scale, y: center.y - 0.65 * scale))
            spark.closeSubpath()
            context.fill(spark, with: .color(Color(red: 0.690196, green: 0.431373, blue: 0.541176)))
        }
    }
}

/// Provider brand glyph — the real logo mark for each AI coding agent, drawn
/// from vector PDFs where the provider supplies one and native geometry for the
/// official Pi/OMP marks where a PDF would add unnecessary raster baggage.
/// Colors and rendering rules are driven by config/provider-brands.json
/// via the generated ProviderBrands enum.
public struct ProviderGlyph: View {
    public enum Variant {
        case chip   // tinted rounded-square background behind the mark
        case bare   // just the mark
    }

    let provider: String?
    let size: CGFloat
    let variant: Variant

    public init(provider: String?, size: CGFloat = 18, variant: Variant = .chip) {
        self.provider = provider
        self.size = size
        self.variant = variant
    }

    private var key: String {
        let raw = (provider ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if raw == "gemini" { return "antigravity" }
        if raw == "z.ai" { return "zai" }
        return raw
    }

    private var assetName: String? {
        switch key {
        case "codex", "openai": return "ProviderCodex"
        case "claude": return "ProviderClaude"
        case "opencode": return "ProviderOpencode"
        case "antigravity": return "ProviderAntigravity"
        case "cursor": return "ProviderCursor"
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

    private var chipStroke: (color: Color, width: Double) {
        let width = config.chipStrokeWidth
        let color: Color
        switch config.chipStrokeType {
        case "solid":
            color = config.chipStrokeColor ?? config.brand.opacity(config.chipStrokeAlpha ?? 0.22)
        case "brand_alpha":
            color = config.brand.opacity(config.chipStrokeAlpha ?? 0.22)
        default:
            color = config.brand.opacity(0.22)
        }
        return (color, width)
    }

    private var chipCornerRadius: CGFloat {
        max(3, size * config.cornerRadiusFactor)
    }

    @ViewBuilder
    private var mark: some View {
        if let assetName {
            if config.glyphStyle == "template", let markColor = config.markColor {
                Image(assetName)
                    .resizable()
                    .renderingMode(.template)
                    .foregroundStyle(markColor)
                    .aspectRatio(contentMode: .fit)
            } else {
                Image(assetName)
                    .resizable()
                    .renderingMode(.original)
                    .aspectRatio(contentMode: .fit)
            }
        } else {
            switch key {
            case "pi":
                PiProviderMark(monochrome: false)
            case "omp":
                OMPProviderMark(monochrome: false)
            case "zai":
                ZAIProviderMark()
            default:
                Image(systemName: "chevron.left.forwardslash.chevron.right")
                    .font(.system(size: size * 0.6, weight: .semibold))
                    .foregroundStyle(.secondary)
            }
        }
    }

    public var body: some View {
        switch variant {
        case .bare:
            mark
                .frame(width: size, height: size)
        case .chip:
            let markSize = size * 0.64
            let stroke = chipStroke
            mark
                .frame(width: markSize, height: markSize)
                .frame(width: size, height: size)
                .background(
                    RoundedRectangle(cornerRadius: chipCornerRadius, style: .continuous)
                        .fill(chipFill)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: chipCornerRadius, style: .continuous)
                        .strokeBorder(stroke.color, lineWidth: stroke.width)
                )
        }
    }
}

/// Proper-cased display name from the generated provider identity contract.
public func providerDisplayLabel(_ provider: String?) -> String {
    ProviderBrands.displayName(provider)
}
