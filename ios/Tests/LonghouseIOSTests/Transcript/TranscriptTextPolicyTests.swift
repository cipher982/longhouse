import Testing
@testable import Longhouse

struct TranscriptTextPolicyTests {
    @Test
    func normalLongParagraphsDoNotCollapse() {
        let text = (1...80)
            .map { "Paragraph \($0): normal response text." }
            .joined(separator: "\n\n")

        #expect(!TranscriptTextPolicy.shouldCollapseMessage(text))
        #expect(TranscriptTextPolicy.visibleMessage(text, expanded: false) == text)
    }

    @Test
    func veryLargeMessagesUseHeadAndTailPreviewUntilExpanded() {
        let text = (1...700)
            .map { "Dump line \($0)" }
            .joined(separator: "\n")
        let visible = TranscriptTextPolicy.visibleMessage(text, expanded: false)

        #expect(TranscriptTextPolicy.shouldCollapseMessage(text))
        #expect(visible.contains("Dump line 1"))
        #expect(visible.contains("Dump line 700"))
        #expect(visible.contains("... 400 lines hidden ..."))
        #expect(!visible.contains("Dump line 350"))
        #expect(TranscriptTextPolicy.visibleMessage(text, expanded: true) == text)
    }
}
