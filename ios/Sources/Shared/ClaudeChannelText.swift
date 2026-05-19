import Foundation

enum ClaudeChannelText {
    static func stripWrapper(_ text: String?) -> String {
        let raw = text ?? ""
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix("<channel"),
              trimmed.hasSuffix("</channel>"),
              let openTagEnd = trimmed.firstIndex(of: ">"),
              let closeTagRange = trimmed.range(of: "</channel>", options: .backwards),
              closeTagRange.upperBound == trimmed.endIndex
        else {
            return raw
        }

        let bodyStart = trimmed.index(after: openTagEnd)
        return String(trimmed[bodyStart..<closeTagRange.lowerBound])
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
