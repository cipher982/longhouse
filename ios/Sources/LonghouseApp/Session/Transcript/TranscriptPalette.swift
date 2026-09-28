import SwiftUI

/// Single source of truth for the transcript's color palette, shared across the
/// Swift/CSS boundary (Phase 1 item #4 — "shared design tokens, ending the
/// double-definition"). The WebKit transcript's CSS `:root`/`@media` block is
/// emitted from here rather than hand-maintained in the HTML string, and the
/// two signal colors that must agree with the native chrome (attention, live)
/// are declared once.
///
/// Discipline: the transcript is the content layer — Ember's ash ladder only.
/// The two signals are `attention` (a dropped/orphaned/failed result — ember)
/// and `live` (the running state dot / capability live-dot — flame).
enum TranscriptPalette {

    // MARK: Cross-boundary signal colors (also used by native chrome)

    /// Attention — a dropped/missing result, waiting on you: Ember's ember.
    /// These hexes are the web-side match (light, dark) of `Ember.ember`.
    static let attentionHexLight = "#bf3e1a"
    static let attentionHexDark = "#e4572e"

    /// Native counterparts of the two signals, so SwiftUI chrome and the web
    /// transcript render the same colors: ember for attention, flame for live.
    static let attention = Ember.ember
    static let live = Ember.flame

    // MARK: CSS variable block (light :root + dark @media), emitted into the doc

    static var cssRootBlock: String {
        """
            :root {
              color-scheme: light dark;
              /* Ember by day: vellum ground, ink text. Color is signal, not
                 decoration: flame is the live node, ember is attention, gold is
                 the link and the brand, brass is the tool trace.
                 Emitted from TranscriptPalette (Swift) — do not hand-edit here. */
              --page: #f3ebdc;
              --text: #2a1f15;
              --primary: #2a1f15;
              --secondary: rgba(107, 88, 68, 0.95);
              --tertiary: rgba(107, 88, 68, 0.62);
              --user: rgba(160, 112, 26, 0.10);
              --user-pending: rgba(160, 112, 26, 0.06);
              --rule: rgba(90, 64, 38, 0.16);
              --trace: rgba(154, 116, 64, 0.50);
              --code: rgba(90, 64, 38, 0.08);
              --attention: \(attentionHexLight);
              --live: #c05f0a;
              --diff-add: #5f6e2c;
              --diff-remove: #bf3e1a;
              --accent: #8f6514;
              --link: #8f6514;
            }

            @media (prefers-color-scheme: dark) {
              :root {
                --page: #0b0908;
                --text: #f6ebd6;
                --primary: #f6ebd6;
                --secondary: rgba(196, 176, 150, 0.92);
                --tertiary: rgba(196, 176, 150, 0.56);
                --user: rgba(233, 185, 73, 0.10);
                --user-pending: rgba(233, 185, 73, 0.06);
                --rule: rgba(246, 235, 214, 0.12);
                --trace: rgba(185, 139, 78, 0.50);
                --code: rgba(246, 235, 214, 0.06);
                --attention: \(attentionHexDark);
                --live: #f08a24;
                --diff-add: #b9c48a;
                --diff-remove: #e87a5c;
                --accent: #e9b949;
                --link: #e9b949;
              }
            }
        """
    }
}
