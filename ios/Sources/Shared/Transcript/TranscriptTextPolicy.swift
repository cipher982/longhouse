enum TranscriptTextPolicy {
    static let messageCollapseLineLimit = 600
    static let messagePreviewHeadLines = 220
    static let messagePreviewTailLines = 80

    static func shouldCollapseMessage(_ text: String) -> Bool {
        lineCount(text) > messageCollapseLineLimit
    }

    static func visibleMessage(_ text: String, expanded: Bool) -> String {
        guard !expanded, shouldCollapseMessage(text) else { return text }
        return sandwichPreview(text)
    }

    static func lineCount(_ text: String) -> Int {
        text.isEmpty ? 0 : text.components(separatedBy: "\n").count
    }

    private static func sandwichPreview(_ text: String) -> String {
        let lines = text.components(separatedBy: "\n")
        guard lines.count > messageCollapseLineLimit else { return text }

        let tailCount = min(messagePreviewTailLines, max(0, lines.count - messagePreviewHeadLines))
        var headEnd = min(messagePreviewHeadLines, lines.count - tailCount)
        let tailStart = lines.count - tailCount

        var fences = 0
        for line in lines.prefix(headEnd) where isFence(line) {
            fences += 1
        }
        if fences % 2 == 1 {
            for index in stride(from: headEnd - 1, through: 0, by: -1) {
                if isFence(lines[index]) {
                    headEnd = index
                    break
                }
            }
        }

        let hiddenLines = max(0, tailStart - headEnd)
        let head = Array(lines.prefix(headEnd))
        let tail = Array(lines.suffix(tailCount))
        return (head + ["", hiddenLineMarker(hiddenLines), ""] + tail).joined(separator: "\n")
    }

    private static func hiddenLineMarker(_ hiddenLines: Int) -> String {
        "... \(hiddenLines) line\(hiddenLines == 1 ? "" : "s") hidden ..."
    }

    private static func isFence(_ line: String) -> Bool {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        return trimmed.hasPrefix("```") || trimmed.hasPrefix("~~~")
    }
}
