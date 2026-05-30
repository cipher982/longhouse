import SwiftUI

/// Single source of truth for the transcript's color palette, shared across the
/// Swift/CSS boundary (Phase 1 item #4 — "shared design tokens, ending the
/// double-definition"). The WebKit transcript's CSS `:root`/`@media` block is
/// emitted from here rather than hand-maintained in the HTML string, and the
/// two signal colors that must agree with the native chrome (attention, live)
/// are declared once.
///
/// Discipline: the transcript is the content layer — neutrals only. The two
/// signals are `attention` (a dropped/failed result — orange) and `live` (the
/// running node / human-message hairline — green). Everything else is grey.
enum TranscriptPalette {

    // MARK: Cross-boundary signal colors (also used by native chrome)

    /// Attention — a dropped/missing result. Native chrome uses system orange;
    /// these hexes are the web-side match (light, dark).
    static let attentionHexLight = "#d68000"
    static let attentionHexDark = "#ff9f0a"

    /// Native counterparts of the two signals, so SwiftUI chrome and the web
    /// transcript render the same colors. System orange/green match the hexes
    /// above closely and adapt to light/dark automatically. (live is the
    /// running state dot; attention is a dropped/failed result.)
    static let attention = Color.orange
    static let live = Color.green

    // MARK: CSS variable block (light :root + dark @media), emitted into the doc

    static var cssRootBlock: String {
        """
            :root {
              color-scheme: light dark;
              /* Monochrome-first: color is signal, not decoration. The transcript
                 is the content layer — system neutrals only. Green appears only as
                 the live node; orange only as attention (a dropped result).
                 Assistant prose has NO container; the human message is the one
                 quiet tinted capsule because it's a rare, injected control action.
                 Emitted from TranscriptPalette (Swift) — do not hand-edit here. */
              --page: #f2f2f7;
              --text: #111114;
              --secondary: rgba(60, 60, 67, 0.68);
              --tertiary: rgba(60, 60, 67, 0.38);
              --user: rgba(120, 120, 128, 0.16);
              --user-pending: rgba(120, 120, 128, 0.10);
              --rule: rgba(60, 60, 67, 0.16);
              --code: rgba(118, 118, 128, 0.12);
              --attention: \(attentionHexLight);
              --link: #006edb;
            }

            @media (prefers-color-scheme: dark) {
              :root {
                --page: #000000;
                --text: #f5f5f7;
                --secondary: rgba(235, 235, 245, 0.62);
                --tertiary: rgba(235, 235, 245, 0.34);
                --user: rgba(120, 120, 128, 0.24);
                --user-pending: rgba(120, 120, 128, 0.16);
                --rule: rgba(235, 235, 245, 0.18);
                --code: rgba(118, 118, 128, 0.24);
                --attention: \(attentionHexDark);
                --link: #65a7ff;
              }
            }
        """
    }
}
