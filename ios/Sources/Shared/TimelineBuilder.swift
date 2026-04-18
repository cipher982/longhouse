import Foundation

enum TimelineItem: Identifiable, Sendable {
    case user(SessionEvent)
    case assistant(SessionEvent)
    case tool(call: SessionEvent, result: SessionEvent?)
    case orphanTool(SessionEvent)

    var id: String {
        switch self {
        case .user(let e): return "user:\(e.id)"
        case .assistant(let e): return "prose:\(e.id)"
        case .tool(let call, _): return "tool:\(call.id)"
        case .orphanTool(let e): return "orphan:\(e.id)"
        }
    }

    var sortTimestamp: String {
        switch self {
        case .user(let e), .assistant(let e), .orphanTool(let e):
            return e.timestamp
        case .tool(let call, _):
            return call.timestamp
        }
    }
}

enum TimelineBuilder {
    /// Build a paired, renderable timeline from raw events.
    /// Mirrors the web pairing logic: assistant-with-tool_name registers in a
    /// Map<tool_call_id, item>. Role=tool events look up their tool_call_id
    /// and attach as the result. Orphan tool events render as their own row.
    static func build(events: [SessionEvent]) -> [TimelineItem] {
        var items: [TimelineItem] = []
        var callIdToIndex: [String: Int] = [:]

        for event in events {
            if event.role == "system" { continue }

            switch event.role {
            case "user":
                items.append(.user(event))

            case "assistant":
                let hasText = !(event.contentText ?? "").isEmpty
                let hasTool = (event.toolName ?? "").isEmpty == false

                if hasText {
                    items.append(.assistant(event))
                }
                if hasTool {
                    items.append(.tool(call: event, result: nil))
                    if let callId = event.toolCallId, !callId.isEmpty {
                        callIdToIndex[callId] = items.count - 1
                    }
                }
                // Assistants with neither text nor tool_name are dropped silently.

            case "tool":
                if let callId = event.toolCallId,
                   let idx = callIdToIndex[callId],
                   case .tool(let call, _) = items[idx] {
                    items[idx] = .tool(call: call, result: event)
                } else {
                    items.append(.orphanTool(event))
                }

            default:
                // Unknown role: treat as assistant prose for safety.
                if !(event.contentText ?? "").isEmpty {
                    items.append(.assistant(event))
                }
            }
        }

        return items
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
