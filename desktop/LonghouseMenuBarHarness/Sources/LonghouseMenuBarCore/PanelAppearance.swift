import SwiftUI

/// Colour scheme for offscreen fixture renders. The live app follows the system.
public enum PanelAppearance: String, CaseIterable, Sendable {
    case dark
    case light

    public var colorScheme: ColorScheme { self == .dark ? .dark : .light }
}
