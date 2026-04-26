enum TranscriptTextPolicy {
    static let messageCollapseCharacterLimit = 40_000

    static func shouldCollapseMessage(_ text: String) -> Bool {
        text.count > messageCollapseCharacterLimit
    }

    static func visibleMessage(_ text: String, expanded: Bool) -> String {
        guard !expanded, shouldCollapseMessage(text) else { return text }
        return String(text.prefix(messageCollapseCharacterLimit))
    }
}
