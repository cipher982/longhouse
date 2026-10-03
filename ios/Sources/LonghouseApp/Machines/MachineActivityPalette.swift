import SwiftUI

/// Activity bars use provider colours tuned for the Ember ground rather than
/// the provider's chip colour. In particular, dark codex and cursor marks are
/// otherwise too close to the chart background to communicate volume.
enum MachineActivityPalette {
    static func color(for provider: String?) -> Color {
        switch ProviderBrands.canonicalKey(provider) {
        case "omp": return Ember.dynamic(dark: 0xA35BF5, light: 0xA35BF5)
        case "claude": return Ember.dynamic(dark: 0xD97757, light: 0xD97757)
        case "codex": return Ember.dynamic(dark: 0x7FB4FF, light: 0x7FB4FF)
        case "cursor": return Ember.dynamic(dark: 0xBDB3A0, light: 0x8C8270)
        case "opencode": return Ember.dynamic(dark: 0x9AA3A8, light: 0x6F777C)
        case "pi": return Ember.dynamic(dark: 0xF1BE58, light: 0xF1BE58)
        case "antigravity": return Ember.dynamic(dark: 0x4F87ED, light: 0x4F87ED)
        case "zai": return Ember.dynamic(dark: 0xB06E8A, light: 0xB06E8A)
        default: return Ember.dynamic(dark: 0x8A7862, light: 0x8A7862)
        }
    }
}
