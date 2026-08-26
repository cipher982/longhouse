import Foundation

/// One worker transcript a tool call spawned. A subagent is a turn artifact of
/// its parent, not a session: hidden from the timeline, reachable only from the
/// row that spawned it.
struct SessionSubagent: Codable, Hashable, Sendable, Identifiable {
    let sessionId: String
    let provider: String
    let parentToolCallId: String?
    let runId: String?
    let startedAt: String?
    let lastActivityAt: String?
    let endedAt: String?
    let userMessages: Int
    let assistantMessages: Int
    let toolCalls: Int
    let title: String?
    let firstUserMessagePreview: String?
    let lastVisibleTextPreview: String?

    var id: String { sessionId }
}

struct SessionSubagentsResponse: Codable, Sendable {
    let sessionId: String
    let children: [SessionSubagent]
}

/// Binding worker transcripts to the tool call that spawned them. Mirrors
/// `web/src/lib/sessionWorkspace/subagents.ts`; the two must agree, because the
/// same session is read on both surfaces.
///
/// Two shapes, two kinds of provider-supplied evidence. A Task/Agent subagent
/// names its parent tool call in a sidecar the engine reads at parse time. A
/// Workflow subagent knows only its run id, and the one place a run is tied to a
/// tool call is the parent's own tool result, which names the transcript
/// directory.
///
/// Fail closed on both: an unmatched run leaves its children unbound and the row
/// renders as an ordinary tool call. Never "nearest Workflow call".
enum Subagents {
    /// Run ids named by one tool result, in the order they appear.
    static func runIds(inToolOutput output: String?) -> [String] {
        guard let output, output.contains("subagents") else { return [] }
        let pattern = #"Transcript dir:\s*\S*?[/\\]subagents[/\\]workflows[/\\]([A-Za-z0-9_-]+)"#
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return [] }
        var found: [String] = []
        let range = NSRange(output.startIndex..<output.endIndex, in: output)
        for match in regex.matches(in: output, range: range) {
            guard match.numberOfRanges > 1, let captured = Range(match.range(at: 1), in: output) else { continue }
            let value = String(output[captured])
            if !value.isEmpty && !found.contains(value) { found.append(value) }
        }
        return found
    }

    /// Children spawned by one tool call: those naming it directly, plus every
    /// member of a run that call launched.
    static func children(
        from children: [SessionSubagent],
        toolCallId: String?,
        toolOutputText: String?
    ) -> [SessionSubagent] {
        guard !children.isEmpty else { return [] }
        let runIds = runIds(inToolOutput: toolOutputText)
        let matched = children.filter { child in
            if let toolCallId, !toolCallId.isEmpty, child.parentToolCallId == toolCallId { return true }
            guard let runId = child.runId else { return false }
            return runIds.contains(runId)
        }
        return matched.sorted { ($0.startedAt ?? "") < ($1.startedAt ?? "") }
    }

    /// "22 agents · 4m12s" — the shape of the work, before anyone expands it.
    static func summary(_ children: [SessionSubagent]) -> String {
        guard !children.isEmpty else { return "" }
        let label = children.count == 1 ? "1 agent" : "\(children.count) agents"
        let starts = children.compactMap { $0.startedAt.flatMap(parseTimestamp) }
        let ends = children.compactMap { $0.endedAt.flatMap(parseTimestamp) }
        guard !starts.isEmpty, ends.count == children.count,
              let first = starts.min(), let last = ends.max() else { return label }
        let span = last.timeIntervalSince(first)
        guard span > 0 else { return label }
        let seconds = Int(span.rounded())
        let duration = seconds < 60 ? "\(seconds)s" : "\(seconds / 60)m\(String(format: "%02d", seconds % 60))s"
        return "\(label) · \(duration)"
    }

    /// One child's line: its own title, or the prompt it was handed.
    static func label(for child: SessionSubagent) -> String {
        let candidate = (child.title ?? child.firstUserMessagePreview ?? "")
            .replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if candidate.isEmpty { return String(child.sessionId.prefix(8)) }
        return candidate.count > 80 ? String(candidate.prefix(79)) + "…" : candidate
    }

    private static func parseTimestamp(_ value: String) -> Date? {
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return withFraction.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    }
}
