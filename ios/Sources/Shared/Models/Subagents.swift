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
    let providerSessionId: String?
    let parentSessionId: String?
    let parentAgentId: String?
    let agentType: String?
    let description: String?
    let model: String?
    let isFork: Bool
    let spawnDepth: Int?
    let finished: Bool?
    let liveStatus: String?
    let durationMs: Int?
    let inputTokens: Int?
    let outputTokens: Int?
    let totalTokens: Int?
    let contextTokens: Int?
    let contextWindow: Int?

    var id: String { sessionId }

    private enum CodingKeys: String, CodingKey {
        case sessionId
        case provider
        case parentToolCallId
        case runId
        case startedAt
        case lastActivityAt
        case endedAt
        case userMessages
        case assistantMessages
        case toolCalls
        case title
        case firstUserMessagePreview
        case lastVisibleTextPreview
        case providerSessionId
        case parentSessionId
        case parentAgentId
        case agentType
        case description
        case model
        case isFork
        case spawnDepth
        case finished
        case liveStatus
        case durationMs
        case inputTokens
        case outputTokens
        case totalTokens
        case contextTokens
        case contextWindow
    }

    init(
        sessionId: String,
        provider: String,
        parentToolCallId: String?,
        runId: String?,
        startedAt: String?,
        lastActivityAt: String?,
        endedAt: String?,
        userMessages: Int,
        assistantMessages: Int,
        toolCalls: Int,
        title: String?,
        firstUserMessagePreview: String?,
        lastVisibleTextPreview: String?,
        providerSessionId: String? = nil,
        parentSessionId: String? = nil,
        parentAgentId: String? = nil,
        agentType: String? = nil,
        description: String? = nil,
        model: String? = nil,
        isFork: Bool = false,
        spawnDepth: Int? = nil,
        finished: Bool? = nil,
        liveStatus: String? = nil,
        durationMs: Int? = nil,
        inputTokens: Int? = nil,
        outputTokens: Int? = nil,
        totalTokens: Int? = nil,
        contextTokens: Int? = nil,
        contextWindow: Int? = nil
    ) {
        self.sessionId = sessionId
        self.provider = provider
        self.parentToolCallId = parentToolCallId
        self.runId = runId
        self.startedAt = startedAt
        self.lastActivityAt = lastActivityAt
        self.endedAt = endedAt
        self.userMessages = userMessages
        self.assistantMessages = assistantMessages
        self.toolCalls = toolCalls
        self.title = title
        self.firstUserMessagePreview = firstUserMessagePreview
        self.lastVisibleTextPreview = lastVisibleTextPreview
        self.providerSessionId = providerSessionId
        self.parentSessionId = parentSessionId
        self.parentAgentId = parentAgentId
        self.agentType = agentType
        self.description = description
        self.model = model
        self.isFork = isFork
        self.spawnDepth = spawnDepth
        self.finished = finished
        self.liveStatus = liveStatus
        self.durationMs = durationMs
        self.inputTokens = inputTokens
        self.outputTokens = outputTokens
        self.totalTokens = totalTokens
        self.contextTokens = contextTokens
        self.contextWindow = contextWindow
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try container.decode(String.self, forKey: .sessionId)
        provider = try container.decode(String.self, forKey: .provider)
        parentToolCallId = try container.decodeIfPresent(String.self, forKey: .parentToolCallId)
        runId = try container.decodeIfPresent(String.self, forKey: .runId)
        startedAt = try container.decodeIfPresent(String.self, forKey: .startedAt)
        lastActivityAt = try container.decodeIfPresent(String.self, forKey: .lastActivityAt)
        endedAt = try container.decodeIfPresent(String.self, forKey: .endedAt)
        userMessages = try container.decodeIfPresent(Int.self, forKey: .userMessages) ?? 0
        assistantMessages = try container.decodeIfPresent(Int.self, forKey: .assistantMessages) ?? 0
        toolCalls = try container.decodeIfPresent(Int.self, forKey: .toolCalls) ?? 0
        title = try container.decodeIfPresent(String.self, forKey: .title)
        firstUserMessagePreview = try container.decodeIfPresent(String.self, forKey: .firstUserMessagePreview)
        lastVisibleTextPreview = try container.decodeIfPresent(String.self, forKey: .lastVisibleTextPreview)
        providerSessionId = try container.decodeIfPresent(String.self, forKey: .providerSessionId)
        parentSessionId = try container.decodeIfPresent(String.self, forKey: .parentSessionId)
        parentAgentId = try container.decodeIfPresent(String.self, forKey: .parentAgentId)
        agentType = try container.decodeIfPresent(String.self, forKey: .agentType)
        description = try container.decodeIfPresent(String.self, forKey: .description)
        model = try container.decodeIfPresent(String.self, forKey: .model)
        isFork = try container.decodeIfPresent(Bool.self, forKey: .isFork) ?? false
        spawnDepth = try container.decodeIfPresent(Int.self, forKey: .spawnDepth)
        finished = try container.decodeIfPresent(Bool.self, forKey: .finished)
        liveStatus = try container.decodeIfPresent(String.self, forKey: .liveStatus)
        durationMs = try container.decodeIfPresent(Int.self, forKey: .durationMs)
        inputTokens = try container.decodeIfPresent(Int.self, forKey: .inputTokens)
        outputTokens = try container.decodeIfPresent(Int.self, forKey: .outputTokens)
        totalTokens = try container.decodeIfPresent(Int.self, forKey: .totalTokens)
        contextTokens = try container.decodeIfPresent(Int.self, forKey: .contextTokens)
        contextWindow = try container.decodeIfPresent(Int.self, forKey: .contextWindow)
    }
}

struct SessionSubagentsResponse: Codable, Sendable {
    let sessionId: String
    let children: [SessionSubagent]
}

/// Binding worker transcripts to the tool call that spawned them. Mirrors
/// `web/src/shared/session/model/subagents.ts`; the two must agree, because the
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

    static func nestedChildren(
        of parent: SessionSubagent,
        in allChildren: [SessionSubagent]
    ) -> [SessionSubagent] {
        guard !allChildren.isEmpty else { return [] }
        let parentKeys = Set(
            [parent.runId, parent.providerSessionId, parent.sessionId]
                .compactMap(normalizedAgentKey)
        )
        return allChildren.filter { candidate in
            guard candidate.sessionId != parent.sessionId else { return false }
            if candidate.parentSessionId == parent.sessionId {
                return true
            }
            if let key = normalizedAgentKey(candidate.parentAgentId) {
                return parentKeys.contains(key)
            }
            return false
        }
        .sorted { ($0.startedAt ?? "") < ($1.startedAt ?? "") }
    }

    private static func normalizedAgentKey(_ value: String?) -> String? {
        guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty else {
            return nil
        }
        if trimmed.hasPrefix("agent-") {
            let stripped = String(trimmed.dropFirst(6))
            return stripped.isEmpty ? nil : stripped
        }
        return trimmed
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
        let preferred = [child.description, child.title, child.firstUserMessagePreview, child.agentType]
            .compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .first { !$0.isEmpty }
        let candidate = (preferred ?? "")
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
