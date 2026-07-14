import SwiftUI

/// Provider brand glyph — the real logo mark for each AI coding agent, drawn
/// from vector PDFs in the asset catalog (ProviderCodex, ProviderClaude,
/// ProviderGemini, ProviderOpencode, ProviderAntigravity).
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
        let raw = (provider ?? "").lowercased()
        return raw == "gemini" ? "antigravity" : raw
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
            Image(systemName: "chevron.left.forwardslash.chevron.right")
                .font(.system(size: size * 0.6, weight: .semibold))
                .foregroundStyle(.secondary)
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

/// Proper-cased display name for a provider. Mirrors web `getProviderLabel`.
public func providerDisplayLabel(_ provider: String?) -> String {
    guard let provider, !provider.isEmpty else { return "Session" }
    switch provider.lowercased() {
    case "codex": return "Codex"
    case "openai": return "OpenAI"
    case "claude": return "Claude"
    case "opencode": return "OpenCode"
    case "cursor": return "Cursor"
    case "gemini", "antigravity": return "Antigravity"
    default: return provider.prefix(1).uppercased() + provider.dropFirst()
    }
}
