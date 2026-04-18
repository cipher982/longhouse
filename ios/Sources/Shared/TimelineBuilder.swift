import Foundation

struct PassiveCall: Identifiable, Sendable {
    let call: SessionEvent
    let result: SessionEvent?
    var id: Int { call.id }
}

enum TimelineItem: Identifiable, Sendable {
    case user(SessionEvent)
    case assistant(SessionEvent)
    case tool(call: SessionEvent, result: SessionEvent?)
    case orphanTool(SessionEvent)
    case passiveGroup(calls: [PassiveCall])

    var id: String {
        switch self {
        case .user(let e): return "user:\(e.id)"
        case .assistant(let e): return "prose:\(e.id)"
        case .tool(let call, _): return "tool:\(call.id)"
        case .orphanTool(let e): return "orphan:\(e.id)"
        case .passiveGroup(let calls):
            let firstId = calls.first?.call.id ?? 0
            return "passive:\(firstId)"
        }
    }

    var sortTimestamp: String {
        switch self {
        case .user(let e), .assistant(let e), .orphanTool(let e):
            return e.timestamp
        case .tool(let call, _):
            return call.timestamp
        case .passiveGroup(let calls):
            return calls.first?.call.timestamp ?? ""
        }
    }
}

enum TimelineBuilder {
    /// Tools that are passive reads/searches — safe to collapse into a single
    /// row when they appear in a run within a turn. Covers both Claude
    /// (Read, Grep, Glob, ...) and Codex (read_file, grep, list_files, ...)
    /// primitives. Bash and Task are deliberately excluded: Bash can do
    /// anything, Task spawns a subagent, and both deserve individual rows.
    static let passiveToolNames: Set<String> = [
        // Claude
        "Read", "Grep", "Glob", "ToolSearch", "WebFetch", "WebSearch",
        // Codex
        "read_file", "grep", "list_files", "find", "codebase_search", "web_search",
    ]

    /// Build a paired, renderable timeline from raw events.
    /// Mirrors the web pairing logic: assistant-with-tool_name registers in a
    /// Map<tool_call_id, item>. Role=tool events look up their tool_call_id
    /// and attach as the result. Orphan tool events render as their own row.
    ///
    /// After pairing, consecutive passive tool calls are collapsed into a
    /// single `.passiveGroup` row. User messages and non-passive tool calls
    /// (Bash, Task, Edit, Write, …) are boundaries that flush the buffer.
    static func build(events: [SessionEvent]) -> [TimelineItem] {
        var raw: [TimelineItem] = []
        var callIdToIndex: [String: Int] = [:]

        for event in events {
            if event.role == "system" { continue }

            switch event.role {
            case "user":
                raw.append(.user(event))

            case "assistant":
                let hasText = !(event.contentText ?? "").isEmpty
                let hasTool = (event.toolName ?? "").isEmpty == false

                if hasText {
                    raw.append(.assistant(event))
                }
                if hasTool {
                    raw.append(.tool(call: event, result: nil))
                    if let callId = event.toolCallId, !callId.isEmpty {
                        callIdToIndex[callId] = raw.count - 1
                    }
                }
                // Assistants with neither text nor tool_name are dropped silently.

            case "tool":
                if let callId = event.toolCallId,
                   let idx = callIdToIndex[callId],
                   case .tool(let call, _) = raw[idx] {
                    raw[idx] = .tool(call: call, result: event)
                } else {
                    raw.append(.orphanTool(event))
                }

            default:
                // Unknown role: treat as assistant prose for safety.
                if !(event.contentText ?? "").isEmpty {
                    raw.append(.assistant(event))
                }
            }
        }

        return collapsePassive(raw)
    }

    /// Collapse runs of 2+ consecutive passive tool calls into `.passiveGroup`
    /// rows. A single passive call stays as `.tool` — one row already, no
    /// need to add an expander. Non-passive items (user, assistant, active
    /// tool calls, orphans) flush the buffer.
    static func collapsePassive(_ items: [TimelineItem]) -> [TimelineItem] {
        var out: [TimelineItem] = []
        var buffer: [PassiveCall] = []

        func flush() {
            guard !buffer.isEmpty else { return }
            if buffer.count == 1 {
                let only = buffer[0]
                out.append(.tool(call: only.call, result: only.result))
            } else {
                out.append(.passiveGroup(calls: buffer))
            }
            buffer.removeAll()
        }

        for item in items {
            switch item {
            case .tool(let call, let result)
                where passiveToolNames.contains(call.toolName ?? ""):
                buffer.append(PassiveCall(call: call, result: result))
            default:
                flush()
                out.append(item)
            }
        }
        flush()
        return out
    }

    /// Extract a one-line human summary from a tool call's input JSON.
    static func inputSummary(for event: SessionEvent) -> String {
        guard let tool = event.toolName else { return "" }
        switch tool {
        case "Bash":
            if let cmd = event.toolInputString("command") {
                return cmd.split(whereSeparator: \.isNewline).first.map(String.init) ?? cmd
            }
            return ""
        case "Grep":
            return event.toolInputString("pattern") ?? ""
        case "Glob":
            return event.toolInputString("pattern") ?? ""
        case "Read", "Edit", "Write", "NotebookEdit":
            if let path = event.toolInputString("file_path") {
                return (path as NSString).lastPathComponent
            }
            return ""
        case "Task":
            if let prompt = event.toolInputString("prompt") {
                return prompt.split(whereSeparator: \.isNewline).first.map(String.init) ?? prompt
            }
            return event.toolInputString("description") ?? ""
        case "WebFetch", "WebSearch":
            return event.toolInputString("url") ?? event.toolInputString("query") ?? ""
        default:
            return ""
        }
    }

    /// Duration between call and result in seconds. nil if pending.
    static func durationSeconds(call: SessionEvent, result: SessionEvent?) -> Double? {
        guard let result else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let fallback = ISO8601DateFormatter()
        fallback.formatOptions = [.withInternetDateTime]
        func parse(_ s: String) -> Date? {
            formatter.date(from: s) ?? fallback.date(from: s)
        }
        guard let a = parse(call.timestamp), let b = parse(result.timestamp) else { return nil }
        return max(0, b.timeIntervalSince(a))
    }

    /// A call without a result is considered "dropped" (rather than still
    /// running) when the enclosing session has terminated, or when the call is
    /// older than 1 hour. 1 hour is a deliberately generous ceiling — longer
    /// than any real tool we run — so legit slow Bash/Task calls aren't falsely
    /// flagged while the session is actively working.
    static let droppedAgeThreshold: TimeInterval = 3600

    static func isDropped(call: SessionEvent, sessionEnded: Bool, now: Date = Date()) -> Bool {
        if sessionEnded { return true }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let fallback = ISO8601DateFormatter()
        fallback.formatOptions = [.withInternetDateTime]
        guard let callDate = formatter.date(from: call.timestamp) ?? fallback.date(from: call.timestamp) else {
            return false
        }
        return now.timeIntervalSince(callDate) > droppedAgeThreshold
    }

    static func formatDuration(_ seconds: Double) -> String {
        if seconds < 1 {
            return "\(Int((seconds * 1000).rounded()))ms"
        } else if seconds < 60 {
            return String(format: "%.1fs", seconds)
        } else {
            let mins = Int(seconds) / 60
            let secs = Int(seconds) % 60
            return "\(mins)m \(secs)s"
        }
    }
}
