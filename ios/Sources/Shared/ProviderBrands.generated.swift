// @generated from schemas/managed_providers.yml and config/provider-brands.json — do not edit by hand.
// Run: python3 scripts/generate/provider_brands.py

import Foundation
import SwiftUI

public struct ProviderBrandConfig: Sendable {
    public let displayName: String
    public let marketingName: String
    public let brand: Color
    public let glyphStyle: String
    public let markColor: Color?
    public let chipFillType: String
    public let chipFillAlpha: Double?
    public let chipFillColor: Color?
    public let chipStrokeType: String
    public let chipStrokeAlpha: Double?
    public let chipStrokeColor: Color?
    public let chipStrokeWidth: Double
    public let cornerRadiusFactor: Double
}

private let defaultConfig = ProviderBrandConfig(
    displayName: "Session",
    marketingName: "Session",
    brand: Color(red: 0.603922, green: 0.560784, blue: 0.494118, opacity: 1),
    glyphStyle: "original",
    markColor: nil,
    chipFillType: "brand_alpha",
    chipFillAlpha: 0.16,
    chipFillColor: nil,
    chipStrokeType: "brand_alpha",
    chipStrokeAlpha: 0.22,
    chipStrokeColor: nil,
    chipStrokeWidth: 0.5,
    cornerRadiusFactor: 0.28,
)

public enum ProviderBrands {
    public static func lookup(_ provider: String?) -> ProviderBrandConfig {
        guard let provider, !provider.isEmpty else { return defaultConfig }
        let raw = provider.lowercased()
        let key = raw == "gemini" ? "antigravity" : raw
        switch key {
        case "claude": return claude
        case "antigravity": return antigravity
        case "gemini": return antigravity
        case "codex": return codex
        case "openai": return codex
        case "opencode": return opencode
        case "cursor": return cursor
        case "zai": return zai
        case "z.ai": return zai
        default: return defaultConfig
        }
    }

    public static func displayName(_ provider: String?, fallback: String = "Session") -> String {
        guard let provider else { return fallback }
        let cleaned = provider.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else { return fallback }
        switch cleaned.lowercased() {
        case "openai": return "OpenAI"
        default:
            let config = lookup(cleaned)
            if config.displayName != defaultConfig.displayName { return config.displayName }
            return cleaned.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    static let claude = ProviderBrandConfig(
        displayName: "Claude",
        marketingName: "Claude Code",
        brand: Color(red: 0.85098, green: 0.466667, blue: 0.341176, opacity: 1),
        glyphStyle: "original",
        markColor: nil,
        chipFillType: "brand_alpha",
        chipFillAlpha: 0.16,
        chipFillColor: nil,
        chipStrokeType: "brand_alpha",
        chipStrokeAlpha: 0.22,
        chipStrokeColor: nil,
        chipStrokeWidth: 0.5,
        cornerRadiusFactor: 0.28,
    )
    static let antigravity = ProviderBrandConfig(
        displayName: "Antigravity",
        marketingName: "Antigravity CLI",
        brand: Color(red: 0.309804, green: 0.529412, blue: 0.929412, opacity: 1),
        glyphStyle: "original",
        markColor: nil,
        chipFillType: "brand_alpha",
        chipFillAlpha: 0.16,
        chipFillColor: nil,
        chipStrokeType: "brand_alpha",
        chipStrokeAlpha: 0.22,
        chipStrokeColor: nil,
        chipStrokeWidth: 0.5,
        cornerRadiusFactor: 0.28,
    )
    static let codex = ProviderBrandConfig(
        displayName: "Codex",
        marketingName: "Codex CLI",
        brand: Color(red: 0.952941, green: 0.917647, blue: 0.85098, opacity: 1),
        glyphStyle: "template",
        markColor: Color(red: 1, green: 1, blue: 1, opacity: 0.921569),
        chipFillType: "solid",
        chipFillAlpha: nil,
        chipFillColor: Color(red: 0.0784314, green: 0.0823529, blue: 0.0901961, opacity: 1),
        chipStrokeType: "solid",
        chipStrokeAlpha: nil,
        chipStrokeColor: Color(red: 1, green: 1, blue: 1, opacity: 0.321569),
        chipStrokeWidth: 0.5,
        cornerRadiusFactor: 0.5,
    )
    static let opencode = ProviderBrandConfig(
        displayName: "OpenCode",
        marketingName: "OpenCode",
        brand: Color(red: 0.788235, green: 0.768627, blue: 0.768627, opacity: 1),
        glyphStyle: "template",
        markColor: Color(red: 0.521569, green: 0.819608, blue: 0.980392, opacity: 1),
        chipFillType: "solid",
        chipFillAlpha: nil,
        chipFillColor: Color(red: 0.121569, green: 0.168627, blue: 0.219608, opacity: 1),
        chipStrokeType: "solid",
        chipStrokeAlpha: nil,
        chipStrokeColor: Color(red: 0.4, green: 0.741176, blue: 0.921569, opacity: 0.45098),
        chipStrokeWidth: 0.5,
        cornerRadiusFactor: 0.18,
    )
    static let cursor = ProviderBrandConfig(
        displayName: "Cursor",
        marketingName: "Cursor Agent",
        brand: Color(red: 0.0784314, green: 0.0705882, blue: 0.0431373, opacity: 1),
        glyphStyle: "template",
        markColor: Color(red: 0.929412, green: 0.92549, blue: 0.92549, opacity: 1),
        chipFillType: "solid",
        chipFillAlpha: nil,
        chipFillColor: Color(red: 0.0784314, green: 0.0705882, blue: 0.0431373, opacity: 1),
        chipStrokeType: "solid",
        chipStrokeAlpha: nil,
        chipStrokeColor: Color(red: 1, green: 1, blue: 1, opacity: 0.278431),
        chipStrokeWidth: 0.5,
        cornerRadiusFactor: 0.28,
    )
    static let zai = ProviderBrandConfig(
        displayName: "Z.ai",
        marketingName: "Z.ai",
        brand: Color(red: 0.690196, green: 0.431373, blue: 0.541176, opacity: 1),
        glyphStyle: "original",
        markColor: nil,
        chipFillType: "brand_alpha",
        chipFillAlpha: 0.16,
        chipFillColor: nil,
        chipStrokeType: "brand_alpha",
        chipStrokeAlpha: 0.22,
        chipStrokeColor: nil,
        chipStrokeWidth: 0.5,
        cornerRadiusFactor: 0.28,
    )
}
