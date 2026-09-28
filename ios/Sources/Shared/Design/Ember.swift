import SwiftUI
import UIKit

/// The Ember palette, shared with the web (`web/src/styles/tokens.css`).
///
/// An ash ladder carries lightness (soot page, char cards, umber raised
/// surfaces, parchment text) and a fire ramp carries meaning: gold is the
/// brand and the primary action, flame is live work, ember is waiting on you
/// or failed, sage is done, and cooled ash is idle or ended. Color is applied
/// tinted, never as a solid fill behind state.
///
/// Dark is the hall at night and matches the web exactly. Light is the same
/// room by day: vellum ground, ink text, and the fire ramp darkened until it
/// holds contrast on paper.
enum Ember {
    // MARK: Ash ladder

    static let page = dynamic(dark: 0x0B0908, light: 0xF3EBDC)
    static let well = dynamic(dark: 0x070605, light: 0xEAE0CC)
    static let card = dynamic(dark: 0x171210, light: 0xFBF6EC)
    static let raised = dynamic(dark: 0x241B16, light: 0xFFFCF6)
    static let border = dynamic(dark: 0x463629, light: 0xD8C8AC)
    static let hairline = dynamic(dark: 0x2B211B, light: 0xE3D6BF)

    // MARK: Text

    static let text = dynamic(dark: 0xF6EBD6, light: 0x2A1F15)
    static let textSecondary = dynamic(dark: 0xC4B096, light: 0x6B5844)
    static let textMuted = dynamic(dark: 0x8A7862, light: 0x98846B)
    static let textInverse = dynamic(dark: 0x0B0908, light: 0x0B0908)

    // MARK: Fire ramp

    /// Brand and primary action. On paper it darkens to brass so tinted text
    /// and glyphs keep contrast; `goldFill` stays the true gold for surfaces.
    static let gold = dynamic(dark: 0xE9B949, light: 0x8F6514)
    static let goldFill = dynamic(dark: 0xE9B949, light: 0xE0AE3E)
    static let goldBright = dynamic(dark: 0xF5D078, light: 0xB9861F)
    static let brass = dynamic(dark: 0xB98B4E, light: 0x9A7440)
    /// Live work: running, thinking, streaming.
    static let flame = dynamic(dark: 0xF08A24, light: 0xC05F0A)
    /// Waiting on you, blocked, failed, stop.
    static let ember = dynamic(dark: 0xE4572E, light: 0xBF3E1A)
    /// Done.
    static let sage = dynamic(dark: 0xB9C48A, light: 0x5F6E2C)
    /// The one cold foil: idle and ended.
    static let ash = dynamic(dark: 0x7C8790, light: 0x6E7881)
    /// Live numerals (timers, counts) in the Nixie capsule.
    static let nixie = dynamic(dark: 0xFFB25A, light: 0xA9570A)

    // MARK: Type

    /// The hall's voice, shared with the web: titles and section heads only.
    /// Never body copy; italic is reserved for the sign-in tagline.
    static func serif(_ size: CGFloat, relativeTo style: Font.TextStyle = .body, italic: Bool = false, bold: Bool = false) -> Font {
        let name: String
        switch (italic, bold) {
        case (false, false): name = "IowanOldStyle-Roman"
        case (false, true): name = "IowanOldStyle-Bold"
        case (true, false): name = "IowanOldStyle-Italic"
        case (true, true): name = "IowanOldStyle-BoldItalic"
        }
        return .custom(name, size: size, relativeTo: style)
    }

    static func serifUIFont(_ size: CGFloat, bold: Bool = false, italic: Bool = false) -> UIFont {
        let name = italic ? (bold ? "IowanOldStyle-BoldItalic" : "IowanOldStyle-Italic")
            : (bold ? "IowanOldStyle-Bold" : "IowanOldStyle-Roman")
        return UIFont(name: name, size: size) ?? .systemFont(ofSize: size, weight: bold ? .bold : .regular)
    }

    // MARK: Helpers

    static func dynamic(dark: UInt32, light: UInt32) -> Color {
        Color(uiColor: dynamicUIColor(dark: dark, light: light))
    }

    static func dynamicUIColor(dark: UInt32, light: UInt32) -> UIColor {
        UIColor { traits in
            uiColor(hex: traits.userInterfaceStyle == .dark ? dark : light)
        }
    }

    static func uiColor(hex: UInt32, alpha: CGFloat = 1) -> UIColor {
        UIColor(
            red: CGFloat((hex >> 16) & 0xFF) / 255,
            green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255,
            alpha: alpha
        )
    }
}

extension Ember {
    /// A fixed (non-adaptive) Ember hex, for materials that already branch on scheme.
    static func uiHex(_ hex: UInt32) -> Color {
        Color(uiColor: uiColor(hex: hex))
    }
}
