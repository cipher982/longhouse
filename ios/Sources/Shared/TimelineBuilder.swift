import Foundation

struct PassiveCall: Identifiable, Sendable {
    let call: SessionEvent
    let result: SessionEvent?
    let pairing: ToolPairing
    var id: Int { call.id }
}

enum ToolPairing: String, Sendable {
    case id
    case fifo
    case pending
}

enum TimelineItem: Identifiable, Sendable {
    case user(SessionEvent)
    case assistant(SessionEvent)
    case tool(call: SessionEvent, result: SessionEvent?, pairing: ToolPairing)
    case orphanTool(SessionEvent)
    case passiveGroup(calls: [PassiveCall])

    var id: String {
        switch self {
        case .user(let e): return "user:\(e.id)"
        case .assistant(let e): return "prose:\(e.id)"
        case .tool(let call, _, _): return "tool:\(call.id)"
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
        case .tool(let call, _, _):
            return call.timestamp
        case .passiveGroup(let calls):
            return calls.first?.call.timestamp ?? ""
        }
    }
}

/// Shared ISO8601 parsing for Longhouse backend timestamps.
///
/// Keep two pre-configured formatters (fractional + plain) to avoid allocating
/// them on every tool-row render. Access stays serialized because the parsers
/// are shared across Swift Testing and timeline refresh tasks.
enum LonghouseDateParser {
    private static let lock = NSLock()
    nonisolated(unsafe) private static let fractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    nonisolated(unsafe) private static let plain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    static func parse(_ s: String) -> Date? {
        lock.lock()
        defer { lock.unlock() }
        return fractional.date(from: s) ?? plain.date(from: s)
    }
}

enum TimelineBuilder {
    /// A tool is "passive" if its tier is `.noise` in config/tool-tiers.json —
    /// a low-signal read/search safe to collapse into a single "Explored" row.
    /// Context- and action-tier tools always render individually.
    /// NOTE: iOS historically collapsed `.context` (Read, WebFetch) as well as
    /// `.noise`, but the shared tier contract keeps context tools as individual
    /// one-liners.
    static func isPassive(_ toolName: String) -> Bool {
        let tier = ToolTiers.tier(toolName)
        return tier == .noise
    }

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
        var fifoToolCallIndexes: [Int] = []

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
                    let pairing: ToolPairing = event.toolCallId?.isEmpty == false ? .id : .pending
                    raw.append(.tool(call: event, result: nil, pairing: pairing))
                    if let callId = event.toolCallId, !callId.isEmpty {
                        callIdToIndex[callId] = raw.count - 1
                    } else {
                        fifoToolCallIndexes.append(raw.count - 1)
                    }
                }
                // Assistants with neither text nor tool_name are dropped silently.

            case "tool":
                var matchedIndex: Int?
                var resultPairing: ToolPairing?
                if let callId = event.toolCallId, !callId.isEmpty {
                    if let idx = callIdToIndex[callId] {
                        matchedIndex = idx
                        resultPairing = .id
                    }
                }
                if matchedIndex == nil,
                   (event.toolCallId ?? "").isEmpty,
                   !fifoToolCallIndexes.isEmpty {
                    matchedIndex = fifoToolCallIndexes.removeFirst()
                    resultPairing = .fifo
                }

                if let idx = matchedIndex,
                   case .tool(let call, _, let pairing) = raw[idx] {
                    raw[idx] = .tool(call: call, result: event, pairing: resultPairing ?? pairing)
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
                out.append(.tool(call: only.call, result: only.result, pairing: only.pairing))
            } else {
                out.append(.passiveGroup(calls: buffer))
            }
            buffer.removeAll()
        }

        for item in items {
            switch item {
            case .tool(let call, let result, let pairing)
                where Self.isPassive(call.toolName ?? ""):
                buffer.append(PassiveCall(call: call, result: result, pairing: pairing))
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
        guard let a = LonghouseDateParser.parse(call.timestamp),
              let b = LonghouseDateParser.parse(result.timestamp) else { return nil }
        return max(0, b.timeIntervalSince(a))
    }

    /// "Dropped" status is server-authoritative: the projection consumes
    /// session ended-ness and call age and emits `tool_call_state == .dropped`.
    /// Clients only consume; they never re-derive.
    static func isDropped(call: SessionEvent) -> Bool {
        call.toolCallState == .dropped
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
