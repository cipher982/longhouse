import Foundation

struct PassiveCall: Identifiable, Sendable {
    let call: SessionEvent
    let result: SessionEvent?
    let pairing: ToolPairing
    var id: String { call.id }
}

enum ToolPairing: String, Sendable {
    case id
    case fifo
    case pending
}

enum TimelineItem: Identifiable, Sendable {
    case user(SessionEvent)
    case assistant(SessionEvent)
    case action(SessionAction, timestamp: String)
    case tool(call: SessionEvent, result: SessionEvent?, pairing: ToolPairing)
    case orphanTool(SessionEvent)
    case passiveGroup(calls: [PassiveCall])

    var id: String {
        switch self {
        case .user(let e): return "user:\(e.id)"
        case .assistant(let e): return "prose:\(e.id)"
        case .action(let action, _): return action.id
        case .tool(let call, _, _): return "tool:\(call.id)"
        case .orphanTool(let e): return "orphan:\(e.id)"
        case .passiveGroup(let calls):
            let firstId = calls.first?.call.id ?? "missing"
            return "passive:\(firstId)"
        }
    }

    var sortTimestamp: String {
        switch self {
        case .user(let e), .assistant(let e), .orphanTool(let e):
            return e.timestamp
        case .action(_, let timestamp):
            return timestamp
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
    static func presentedToolName(_ event: SessionEvent) -> String {
        event.toolPresentation?.toolName ?? event.toolName ?? "Tool"
    }

    static func presentationAggregate(_ event: SessionEvent) -> ToolAggregate? {
        if let raw = event.toolPresentation?.aggregate, let aggregate = ToolAggregate(rawValue: raw) {
            return aggregate
        }
        return ToolTiers.aggregate(presentedToolName(event))
    }

    static func presentedToolInputString(_ event: SessionEvent, _ key: String) -> String? {
        if let toolInput = event.toolPresentation?.toolInputValue,
           case .object(let value) = toolInput {
            switch value[key] {
            case .string(let text): return text
            case .int(let number): return String(number)
            case .double(let number): return String(number)
            case .bool(let value): return String(value)
            case .array, .object, .null, .none: break
            }
        }
        return event.toolInputString(key)
    }

    /// Exploration-eligible tools may join consecutive `.passiveGroup` rows.
    /// Eligibility comes from `ToolTiers.aggregate`, not display tier — so
    /// completed Reads can join Greps while singleton Reads stay individual.
    static func isExplorationEligible(call: SessionEvent, result: SessionEvent?, pairing: ToolPairing) -> Bool {
        guard !(presentedToolName(call)).isEmpty else { return false }
        guard presentationAggregate(call) != nil || shellSalience(call: call, result: result) != nil else {
            return false
        }
        guard pairing == .id || pairing == .fifo else { return false }
        guard result != nil else { return false }
        if call.toolCallState == .dropped || call.toolCallState == .running {
            return false
        }
        if let exit = ShellSalienceClassifier.parseExitCode(result?.toolOutputText), exit != 0 {
            return false
        }
        return true
    }

    /// Content-aware demotion for shell tools (Change B). A completed,
    /// successful shell call whose command classifies as read-only may join
    /// exploration runs. Nonzero exits never demote — errors stay full-size.
    /// Mirrors web `getShellSalience` in timelineModel.ts.
    static func shellSalience(call: SessionEvent, result: SessionEvent?) -> ShellSalience? {
        let toolName = presentedToolName(call)
        guard ShellSalienceClassifier.isShellTool(toolName) else { return nil }
        guard let result else { return nil }
        if call.toolCallState == .dropped || call.toolCallState == .running { return nil }
        if let exit = ShellSalienceClassifier.parseExitCode(result.toolOutputText), exit != 0 { return nil }
        let command = presentedToolInputString(call, "command") ?? presentedToolInputString(call, "cmd")
        return ShellSalienceClassifier.classify(command)
    }

    /// Semantic exploration header: `Searched 5 · Read 14 · Listed 1`.
    static func explorationSummary(for calls: [PassiveCall]) -> String {
        var searched = 0
        var read = 0
        var listed = 0
        var waited = 0
        for call in calls {
            let aggregate = shellSalience(call: call.call, result: call.result)?.aggregate
                ?? presentationAggregate(call.call)
            switch aggregate {
            case .search: searched += 1
            case .read: read += 1
            case .list: listed += 1
            case .wait: waited += 1
            case .none: break
            }
        }
        var parts: [String] = []
        if searched > 0 { parts.append("Searched \(searched)") }
        if read > 0 { parts.append("Read \(read)") }
        if listed > 0 { parts.append("Listed \(listed)") }
        if waited > 0 { parts.append("Waited \(waited)") }
        return parts.joined(separator: " · ")
    }

    static let explorationOverflowVisible = 8

    static func splitExplorationOverflow<T>(_ items: [T], visible: Int = explorationOverflowVisible) -> (earlier: [T], latest: [T]) {
        guard items.count > visible else { return ([], items) }
        let idx = items.count - visible
        return (Array(items.prefix(idx)), Array(items.suffix(visible)))
    }

    /// Legacy name — true when the tool has an aggregate category.
    static func isPassive(_ toolName: String) -> Bool {
        ToolTiers.aggregate(toolName) != nil
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

    static func build(items projectionItems: [SessionProjectionItem]) -> [TimelineItem] {
        var out: [TimelineItem] = []
        var eventBuffer: [SessionEvent] = []

        func flushEvents() {
            guard !eventBuffer.isEmpty else { return }
            out.append(contentsOf: build(events: eventBuffer))
            eventBuffer.removeAll()
        }

        for item in projectionItems {
            if item.kind == "seam" {
                // Seams are presentation boundaries even when this builder does
                // not render a dedicated seam row — flush so exploration runs
                // cannot span across them (parity with web).
                flushEvents()
                continue
            }
            if item.kind == "action", let action = item.action {
                flushEvents()
                out.append(.action(action, timestamp: item.timestamp))
                continue
            }
            if item.kind == "event", let event = item.event {
                eventBuffer.append(event)
            }
        }

        flushEvents()
        return out
    }

    /// Collapse runs of 2+ consecutive exploration-eligible tool calls into
    /// `.passiveGroup` rows. A single eligible call stays as `.tool`. Breakers
    /// (user/assistant prose, ineligible tools, orphans, actions) flush.
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
                where Self.isExplorationEligible(call: call, result: result, pairing: pairing):
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
        let tool = presentedToolName(event)
        if let children = event.toolPresentation?.children, !children.isEmpty,
           event.toolPresentation?.wrapperRecedes != true {
            return "contains " + children.map(\.label).joined(separator: " · ")
        }
        switch tool {
        case "Bash", "shell", "shell_command", "exec_command", "run_shell_command":
            if let cmd = presentedToolInputString(event, "command") ?? presentedToolInputString(event, "cmd") {
                return cmd.split(whereSeparator: \.isNewline).first.map(String.init) ?? cmd
            }
            return ""
        case "Grep":
            return presentedToolInputString(event, "pattern") ?? ""
        case "Glob":
            return presentedToolInputString(event, "pattern") ?? ""
        case "Read", "Edit", "Write", "NotebookEdit":
            if let path = presentedToolInputString(event, "file_path") ?? presentedToolInputString(event, "path") {
                return (path as NSString).lastPathComponent
            }
            return ""
        case "apply_patch":
            guard let patch = presentedToolInputString(event, "patch") else { return "Applied patch" }
            let prefixes = ["*** Update File: ", "*** Add File: ", "*** Delete File: "]
            let paths = patch.split(separator: "\n").compactMap { line -> String? in
                for prefix in prefixes where line.hasPrefix(prefix) {
                    return String(line.dropFirst(prefix.count)).trimmingCharacters(in: .whitespacesAndNewlines)
                }
                return nil
            }
            guard let first = paths.first else { return "Applied patch" }
            let extra = paths.count - 1
            let suffix = extra > 0 ? " + \(extra) \(extra == 1 ? "file" : "files")" : ""
            return (first as NSString).lastPathComponent + suffix
        case "Task":
            if let prompt = presentedToolInputString(event, "prompt") {
                return prompt.split(whereSeparator: \.isNewline).first.map(String.init) ?? prompt
            }
            return presentedToolInputString(event, "description") ?? ""
        case "WebFetch", "WebSearch":
            return presentedToolInputString(event, "url") ?? presentedToolInputString(event, "query") ?? ""
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
