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
    func veryLargeMessagesCollapseUntilExpanded() {
        let text = String(repeating: "x", count: TranscriptTextPolicy.messageCollapseCharacterLimit + 1)

        #expect(TranscriptTextPolicy.shouldCollapseMessage(text))
        #expect(TranscriptTextPolicy.visibleMessage(text, expanded: false).count == TranscriptTextPolicy.messageCollapseCharacterLimit)
        #expect(TranscriptTextPolicy.visibleMessage(text, expanded: true) == text)
    }
}
