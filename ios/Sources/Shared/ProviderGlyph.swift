import SwiftUI

/// Provider brand glyph — the real logo mark for each AI coding agent, drawn
/// from vector PDFs in the asset catalog (ProviderCodex, ProviderClaude,
/// ProviderGemini, ProviderOpencode, ProviderAntigravity).
///
/// Mirrors the web `<ProviderGlyph>` (web/src/components/ProviderGlyph.tsx).
/// Two non-overlapping color channels, same discipline as the web app and the
/// transcript palette: the glyph carries PROVIDER IDENTITY (the provider's real
/// brand color, baked into the PDF), while runtime/status color lives on the
/// liveness dot and never on the glyph.
///
/// The PDFs are full-color (Gemini is a real gradient), so the asset uses
/// `template-rendering-intent = original` and we do NOT apply a tint.
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
        (provider ?? "").lowercased()
    }

    private var assetName: String? {
        switch key {
        case "codex", "openai": return "ProviderCodex"
        case "claude": return "ProviderClaude"
        case "gemini": return "ProviderGemini"
        case "opencode": return "ProviderOpencode"
        case "antigravity": return "ProviderAntigravity"
        default: return nil
        }
    }

    /// Real brand tint, used only for the chip background wash (not the mark).
    private var brand: Color {
        switch key {
        case "claude": return Color(red: 0xD9 / 255, green: 0x77 / 255, blue: 0x57 / 255)
        case "codex", "openai": return Color(white: 0.92)
        case "opencode": return Color(white: 0.78)
        case "antigravity": return Color(red: 0x4F / 255, green: 0x87 / 255, blue: 0xED / 255)
        case "gemini": return Color(red: 0x8E / 255, green: 0x75 / 255, blue: 0xB2 / 255)
        default: return .secondary
        }
    }

    @ViewBuilder
    private var mark: some View {
        if let assetName {
            Image(assetName)
                .resizable()
                .renderingMode(.original)
                .aspectRatio(contentMode: .fit)
        } else {
            // Unknown provider — neutral terminal chevron fallback.
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
    case "gemini": return "Gemini"
    case "opencode": return "OpenCode"
    case "antigravity": return "Antigravity"
    default: return provider.prefix(1).uppercased() + provider.dropFirst()
    }
}
