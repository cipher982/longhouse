import XCTest
@testable import Longhouse

/// Locks the Commit 3 WebKit CSS restyle decisions so they can't silently
/// revert: assistant prose is a plain document (no card), chat bubbles are
/// gone, the palette is monochrome (no purple/blue tool/user tints), tool rows
/// are demoted (no boxed purple background), and a dropped tool result is
/// flagged in attention color. Asserted against the static document HTML so it
/// runs without a WebView.
@MainActor
final class TranscriptStyleContractTests: XCTestCase {

    private var css: String { WebTranscriptView.documentHTMLForTesting }

    // MARK: Monochrome palette — the old decorative tints are gone

    func testNoPurpleToolTint() {
        XCTAssertFalse(css.contains("120, 82, 180"), "Light purple tool tint must be removed")
        XCTAssertFalse(css.contains("167, 139, 250"), "Dark purple tool tint must be removed")
    }

    func testNoBlueUserBubbleTint() {
        XCTAssertFalse(css.contains("0, 122, 255"), "Blue user-bubble tint must be removed")
        XCTAssertFalse(css.contains("10, 132, 255"), "Dark blue user-bubble tint must be removed")
    }

    func testOldTokensRemoved() {
        XCTAssertFalse(css.contains("--assistant:"), "Assistant card token must be gone (prose has no card)")
        XCTAssertFalse(css.contains("--tool:"), "Tool tint token must be gone")
        XCTAssertFalse(css.contains("--tool-border:"), "Tool border token must be gone")
    }

    // MARK: New monochrome / signal tokens exist

    func testNeutralAndSignalTokensPresent() {
        XCTAssertTrue(css.contains("--rule:"), "Neutral rule token should drive separators")
        XCTAssertTrue(css.contains("--attention:"), "Attention signal token should exist for dropped results")
    }

    // The human-message capsule must NOT borrow the live-signal (green) color —
    // right-alignment + neutral fill is the "this is you" signal, not an outline.
    func testHumanMessageHasNoGreenSignalOutline() {
        XCTAssertFalse(css.contains("--user-hairline"), "Green hairline token must be removed")
        guard let block = css.range(of: #"\.bubble \{[^}]*\}"#, options: .regularExpression).map({ String(css[$0]) }) else {
            return XCTFail(".bubble rule not found")
        }
        XCTAssertFalse(block.contains("box-shadow"), "Human capsule must not carry a signal-color outline")
    }

    // MARK: Assistant prose is a plain document — no card background

    func testAssistantHasNoCardBackground() {
        // The assistant rule must explicitly null out padding+background.
        XCTAssertTrue(
            css.contains(".message.assistant {") &&
            css.range(of: #"\.message\.assistant \{[^}]*background: transparent;"#, options: .regularExpression) != nil,
            "Assistant prose must render without a card background"
        )
    }

    // MARK: Tool rows demoted — no boxed/filled background, grouped by a rule

    func testToolRowsAreDemotedNotBoxed() {
        guard let block = css.range(of: #"details\.tool, details\.passive \{[^}]*\}"#, options: .regularExpression).map({ String(css[$0]) }) else {
            return XCTFail("tool/passive details block not found")
        }
        XCTAssertTrue(block.contains("background: transparent;"), "Tool rows must not have a filled background")
        XCTAssertTrue(block.contains("border-left"), "Tool rows should be grouped by a left rule, not a box")
        XCTAssertFalse(block.contains("var(--tool)"), "Tool rows must not use the old purple tint")
    }

    // MARK: Dropped AND orphan results are loud (attention color)

    func testDroppedAndOrphanToolResultsUseAttentionColor() {
        // Both dropped and orphan share one attention rule — "result missing".
        guard let block = css.range(
            of: #"\.tool-meta\.dropped[^{]*\{[^}]*\}"#,
            options: .regularExpression
        ).map({ String(css[$0]) }) else {
            return XCTFail(".tool-meta.dropped rule not found")
        }
        XCTAssertTrue(block.contains("var(--attention)"), "Dropped/orphan must be flagged in attention color")
        XCTAssertTrue(css.contains(".tool-meta.orphan"), "Orphan results must share the attention treatment, not render grey")
    }

    // MARK: Table rendering — CSS tokens and JS helpers

    func testTableCSSPresent() {
        XCTAssertTrue(css.contains(".table-wrap {"), "Scrollable table wrapper CSS must exist")
        XCTAssertTrue(css.contains("border-collapse: collapse;"), "Table must collapse borders")
        XCTAssertTrue(css.contains("var(--rule)"), "Table borders must use the --rule palette token")
    }

    func testH3StylePresent() {
        XCTAssertTrue(css.contains("h3 {"), "h3 CSS rule must exist")
        XCTAssertTrue(css.contains("font-size: 15px;"), "h3 must have a distinct font size")
    }

    func testTableJSHelpersPresent() {
        XCTAssertTrue(css.contains("function isTableSeparator"), "isTableSeparator JS helper must be present")
        XCTAssertTrue(css.contains("function isTableRow"), "isTableRow JS helper must be present")
        XCTAssertTrue(css.contains("function tableToHtml"), "tableToHtml JS helper must be present")
        XCTAssertTrue(css.contains("function splitCells"), "splitCells JS helper must be present")
    }

    func testMarkdownToHtmlHandlesTableAndH3() {
        // Verify the JS function body references table state and h3
        XCTAssertTrue(css.contains("tableRows"), "markdownToHtml must accumulate table rows")
        XCTAssertTrue(css.contains("### "), "markdownToHtml must handle h3 prefix")
    }

    // MARK: Bottom-inset hook for the floating control surface is wired

    func testBottomInsetVariableDrivesRootPadding() {
        XCTAssertTrue(css.contains("var(--native-bottom-inset"), "Root padding must read the native bottom inset var")
        XCTAssertTrue(css.contains("window.setBottomInset"), "JS bottom-inset setter must exist for the floating card")
    }

    // MARK: Shared design tokens — the palette is the single source of truth

    func testPaletteBlockIsSplicedNotLeftAsMarker() {
        XCTAssertFalse(css.contains("__LH_ROOT_BLOCK__"), "Palette marker must be replaced, not shipped raw")
        XCTAssertTrue(css.contains(":root {"), "Assembled doc must contain the :root block")
    }

    func testAttentionColorComesFromPalette() {
        // The CSS attention var must match the Swift palette's declared hexes,
        // proving the Swift/CSS double-definition is actually unified.
        XCTAssertTrue(css.contains("--attention: \(TranscriptPalette.attentionHexLight)"))
        XCTAssertTrue(css.contains("--attention: \(TranscriptPalette.attentionHexDark)"))
    }

    // MARK: The human-message capsule still exists (preserved, neutral fill)

    func testHumanMessageCapsulePreserved() {
        guard let block = css.range(of: #"\.bubble \{[^}]*\}"#, options: .regularExpression).map({ String(css[$0]) }) else {
            return XCTFail(".bubble rule not found")
        }
        XCTAssertTrue(block.contains("var(--user)"), "Human message keeps a neutral capsule fill")
    }
}
