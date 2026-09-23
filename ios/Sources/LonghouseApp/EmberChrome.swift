import SwiftUI
import UIKit

/// App-wide Ember chrome: gold as the one accent, parchment text, and the
/// hall's serif on navigation titles. Screens still own their backgrounds.
enum EmberAppearance {
    @MainActor
    static func install() {
        let bar = UINavigationBar.appearance()
        // Scaled to the reader's text size at launch (Dynamic Type).
        bar.largeTitleTextAttributes = [
            .font: UIFontMetrics(forTextStyle: .largeTitle).scaledFont(for: Ember.serifUIFont(34)),
            .foregroundColor: Ember.dynamicUIColor(dark: 0xF6EBD6, light: 0x2A1F15),
        ]
        bar.titleTextAttributes = [
            .font: UIFont.preferredFont(forTextStyle: .headline),
            .foregroundColor: Ember.dynamicUIColor(dark: 0xF6EBD6, light: 0x2A1F15),
        ]
    }
}

private struct EmberChromeModifier: ViewModifier {
    func body(content: Content) -> some View {
        content
            .tint(Ember.gold)
            // Hierarchical levels: `.secondary` reads as clay and `.tertiary`
            // as muted ash, warm hues rather than faded parchment. This also
            // wins over tint for plain Form/List buttons, so an action row
            // there sets `Ember.gold` (or `Ember.ember` if destructive) itself.
            .foregroundStyle(Ember.text, Ember.textSecondary, Ember.textMuted)
    }
}

extension View {
    func emberChrome() -> some View {
        modifier(EmberChromeModifier())
    }
}

/// A section head in the hall's voice: serif title, a quiet count, and a rule
/// that starts gold and fades, as on the web timeline.
struct EmberSectionHeader: View {
    let title: String
    let count: Int?
    var italic: Bool = false
    var size: CGFloat = 21

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(title)
                .font(Ember.serif(size, relativeTo: .title3, italic: italic))
                .foregroundStyle(Ember.text)
                .accessibilityAddTraits(.isHeader)
            if let count {
                Text("\(count)")
                    .font(.footnote.weight(.medium))
                    .monospacedDigit()
                    .foregroundStyle(Ember.textMuted)
            }
            LinearGradient(
                colors: [Ember.gold.opacity(0.55), Ember.gold.opacity(0)],
                startPoint: .leading,
                endPoint: .trailing
            )
            .frame(height: 1)
            .alignmentGuide(.firstTextBaseline) { d in d[.bottom] + 6 }
        }
        .padding(.horizontal, 2)
    }
}

/// The page ground: soot (or vellum by day) under a warm light falling from
/// the top edge, the web's hearth light.
struct EmberHearthBackground: View {
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        ZStack {
            Ember.page
            RadialGradient(
                colors: [
                    Ember.goldFill.opacity(colorScheme == .dark ? 0.11 : 0.16),
                    Ember.goldFill.opacity(0),
                ],
                center: UnitPoint(x: 0.5, y: -0.08),
                startRadius: 0,
                endRadius: 460
            )
        }
        .ignoresSafeArea()
    }
}

extension View {
    /// The one prominent action in a toolbar (start a session): a gold glyph
    /// in the shared glass group, the web's gold-wash rule rather than a solid
    /// sticker.
    func emberProminentToolbarButton() -> some View {
        self.foregroundStyle(Ember.gold)
    }
}

/// The one primary action on a screen (Start session): true gold with soot
/// ink, so it holds contrast in both schemes. Disabled drops to a well.
struct EmberPrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.body.weight(.semibold))
            .foregroundStyle(isEnabled ? Ember.gold : Ember.textMuted)
            .padding(.vertical, 15)
            .frame(maxWidth: .infinity)
            .background(Capsule(style: .continuous).fill(isEnabled ? Ember.goldFill.opacity(0.17) : Ember.well))
            .overlay {
                Capsule(style: .continuous)
                    .strokeBorder(isEnabled ? Ember.gold.opacity(0.55) : Ember.hairline, lineWidth: 1)
            }
            .opacity(configuration.isPressed ? 0.8 : 1)
    }
}

extension View {
    /// Grouped Form/List on the Ember ground; sections still set their own
    /// `listRowBackground(Ember.card)`.
    func emberListGround() -> some View {
        scrollContentBackground(.hidden)
            .background(Ember.page)
    }
}

/// A live quantity (turn timer, count) in the web's Nixie capsule: amber mono
/// digits with a soft flame glow inside a brass-rimmed capsule. Dead values
/// keep the capsule without the glow.
struct NixieReadout: View {
    let text: String
    let live: Bool
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        Text(text)
            .font(.system(.subheadline, design: .monospaced).weight(.medium))
            .monospacedDigit()
            .kerning(0.4)
            .foregroundStyle(live ? Ember.nixie : Ember.textSecondary)
            .shadow(color: live && colorScheme == .dark ? Ember.flame.opacity(0.55) : .clear, radius: 5)
            .padding(.horizontal, 9)
            .padding(.vertical, 2)
            .background(Capsule(style: .continuous).fill(Ember.text.opacity(0.045)))
            .overlay {
                Capsule(style: .continuous)
                    .strokeBorder(Ember.gold.opacity(live ? 0.34 : 0.18), lineWidth: 0.75)
            }
    }
}

/// Four brass points at the corners of the one framed object (the dock), as
/// on the web composer.
struct BrassCornerPoints: View {
    var inset: CGFloat = 7
    var body: some View {
        GeometryReader { proxy in
            let w = proxy.size.width, h = proxy.size.height
            ForEach(0..<4, id: \.self) { index in
                Circle()
                    .fill(Ember.brass.opacity(0.75))
                    .frame(width: 3, height: 3)
                    .position(
                        x: index % 2 == 0 ? inset : w - inset,
                        y: index < 2 ? inset : h - inset
                    )
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}
