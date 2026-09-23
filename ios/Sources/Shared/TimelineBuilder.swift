import Foundation

struct ActivityCall: Identifiable, Sendable {
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
    case providerNotification(SessionEvent)
    case action(SessionAction, timestamp: String)
    case tool(call: SessionEvent, result: SessionEvent?, pairing: ToolPairing)
    case orphanTool(SessionEvent)
    case activityGroup(calls: [ActivityCall])

    var id: String {
        switch self {
        case .user(let e): return "user:\(e.id)"
        case .assistant(let e): return "prose:\(e.id)"
        case .providerNotification(let e): return "provider-notification:\(e.id)"
        case .action(let action, _): return action.id
        case .tool(let call, _, _): return "tool:\(call.id)"
        case .orphanTool(let e): return "orphan:\(e.id)"
        case .activityGroup(let calls):
            let firstId = calls.first?.call.id ?? "missing"
            return "activity:\(firstId)"
        }
    }

    var sortTimestamp: String {
        switch self {
        case .user(let e), .assistant(let e), .orphanTool(let e):
            return e.timestamp
        case .providerNotification(let e):
            return e.timestamp
        case .action(_, let timestamp):
            return timestamp
        case .tool(let call, _, _):
            return call.timestamp
        case .activityGroup(let calls):
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
        if let date = parseInternetDateTime(s) { return date }
        lock.lock()
        defer { lock.unlock() }
        return fractional.date(from: s) ?? plain.date(from: s)
    }

    /// `YYYY-MM-DDTHH:MM:SS[.fff…](Z|±HH:MM|±HHMM)`, the only shape the server
    /// emits, parsed without ICU. ISO8601DateFormatter costs microseconds per
    /// call and timeline sorting calls this per comparison: 0.7 s of main
    /// thread on a device cold launch. Anything else falls through to the
    /// formatters, so accepted input is unchanged.
    static func parseInternetDateTime(_ string: String) -> Date? {
        var string = string
        return string.withUTF8 { b -> Date? in
            let n = b.count
            guard n >= 20 else { return nil }
            func number(_ at: Int, _ width: Int) -> Int? {
                guard at + width <= n else { return nil }
                var value = 0
                for k in at..<(at + width) {
                    let digit = b[k] &- 48
                    guard digit < 10 else { return nil }
                    value = value * 10 + Int(digit)
                }
                return value
            }
            guard b[4] == 0x2D, b[7] == 0x2D, b[10] == 0x54, b[13] == 0x3A, b[16] == 0x3A,
                  let year = number(0, 4), let month = number(5, 2), let day = number(8, 2),
                  let hour = number(11, 2), let minute = number(14, 2), let second = number(17, 2),
                  (1...12).contains(month), day >= 1, hour < 24, minute < 60, second < 60
            else { return nil }
            // Impossible dates (Feb 30, Feb 29 in a common year) fall through
            // to the formatter rather than rolling into the next month.
            let isLeap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
            let monthDays = [31, isLeap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
            guard day <= monthDays[month - 1] else { return nil }
            var i = 19
            var fraction = 0.0
            if b[i] == 0x2E {
                i += 1
                let start = i
                var scale = 0.1
                while i < n, b[i] &- 48 < 10 {
                    fraction += Double(b[i] &- 48) * scale
                    scale /= 10
                    i += 1
                }
                guard i > start else { return nil }
            }
            guard i < n else { return nil }
            var offset = 0
            switch b[i] {
            case 0x5A:
                i += 1
            case 0x2B, 0x2D:
                let sign = b[i] == 0x2B ? 1 : -1
                i += 1
                guard let offsetHours = number(i, 2) else { return nil }
                i += 2
                if i < n, b[i] == 0x3A { i += 1 }
                guard let offsetMinutes = number(i, 2), offsetHours < 24, offsetMinutes < 60 else { return nil }
                i += 2
                offset = sign * (offsetHours * 3600 + offsetMinutes * 60)
            default:
                return nil
            }
            guard i == n else { return nil }
            // Days from 1970-01-01 in the proleptic Gregorian calendar
            // (Howard Hinnant's days_from_civil).
            let y = month <= 2 ? year - 1 : year
            let era = y / 400
            let yearOfEra = y - era * 400
            let dayOfYear = (153 * ((month + 9) % 12) + 2) / 5 + day - 1
            let dayOfEra = yearOfEra * 365 + yearOfEra / 4 - yearOfEra / 100 + dayOfYear
            let days = era * 146_097 + dayOfEra - 719_468
            let seconds = days * 86_400 + hour * 3600 + minute * 60 + second - offset
            return Date(timeIntervalSince1970: Double(seconds) + fraction)
        }
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

    private static let humanInteractionTools: Set<String> = [
        "askuserquestion", "request_user_input", "request_permissions",
        "request_permission", "request_approval", "approval_request",
    ]

    static func hasStructuredFailure(_ output: String?) -> Bool {
        guard let output else { return false }
        let text = output.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.lowercased().hasPrefix("[tool error]") { return true }
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return false }
        let exitCode = object["exit_code"] as? Int ?? object["exitCode"] as? Int
        return object["ok"] as? Bool == false
            || object["success"] as? Bool == false
            || object["is_error"] as? Bool == true
            || (exitCode != nil && exitCode != 0)
    }

    /// One predicate drives group exclusion, styling, the `exit N` chip, and
    /// the inline failure preview. Mirrors `isToolInteractionFailed()` on web.
    /// Dropped and orphan are deliberately NOT failures: they already have
    /// their own chip and styling, and their "output" is a placeholder, not a
    /// command's error text.
    static func isFailed(call: SessionEvent, result: SessionEvent?) -> Bool {
        if let exit = ShellSalienceClassifier.parseExitCode(result?.toolOutputText), exit != 0 {
            return true
        }
        return hasStructuredFailure(result?.toolOutputText)
    }

    /// Edit-category membership. Diff stats are only meaningful for edits, so
    /// render paths gate on this rather than on "the input happens to carry a
    /// text-ish key". Mirrors `isEditInteraction()` on web.
    static func isEditInteraction(_ event: SessionEvent) -> Bool {
        if presentationAggregate(event) != nil { return false }
        let identity = ([presentedToolName(event), event.toolPresentation?.label]
            .compactMap { $0 }).joined(separator: " ").lowercased()
            .replacingOccurrences(of: #"[^a-z0-9]+"#, with: " ", options: .regularExpression)
        return identity.range(
            of: #"\b(edit|edited|write|patch|replace|notebook|create file|write to file)\b"#,
            options: .regularExpression
        ) != nil
    }

    /// Bounds for the inline failure preview on a collapsed row.
    static let failurePreviewHeadLines = 2
    static let failurePreviewTailLines = 8
    static let failurePreviewMaxChars = 4096

    /// A failed command's output is not re-derivable, so a collapsed row shows
    /// a bounded preview without a tap. Head lines are kept as well as tail
    /// lines: an exception heading or a single-line megabyte JSON error would
    /// be lost entirely to a pure tail.
    static func failurePreview(call: SessionEvent, result: SessionEvent?) -> String? {
        guard isFailed(call: call, result: result) else { return nil }
        guard let raw = result?.toolOutputText else { return nil }
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }

        let lines = text.components(separatedBy: "\n")
        var preview: String
        if lines.count <= failurePreviewHeadLines + failurePreviewTailLines + 1 {
            preview = text
        } else {
            let head = lines.prefix(failurePreviewHeadLines)
            let tail = lines.suffix(failurePreviewTailLines)
            let elided = lines.count - head.count - tail.count
            preview = (Array(head) + ["… \(elided) more lines …"] + Array(tail))
                .joined(separator: "\n")
        }
        if preview.count > failurePreviewMaxChars {
            preview = String(preview.prefix(failurePreviewMaxChars)) + "\n… truncated …"
        }
        return preview
    }

    /// Completed, attributable tools may join prose-bounded activity groups.
    static func isActivityEligible(call: SessionEvent, result: SessionEvent?, pairing: ToolPairing) -> Bool {
        guard !(presentedToolName(call)).isEmpty else { return false }
        guard pairing == .id || pairing == .fifo else { return false }
        guard result != nil else { return false }
        if call.toolCallState == .dropped || call.toolCallState == .running {
            return false
        }
        if let exit = ShellSalienceClassifier.parseExitCode(result?.toolOutputText), exit != 0 {
            return false
        }
        if hasStructuredFailure(result?.toolOutputText) { return false }
        let exactNames = [call.toolName, call.toolPresentation?.toolName]
            .compactMap { $0?.lowercased() }
        if exactNames.contains(where: humanInteractionTools.contains) { return false }
        let identity = ([presentedToolName(call), call.toolPresentation?.label].compactMap { $0 })
            .joined(separator: " ").lowercased()
        if identity.range(of: #"\b(question|approval|permission)\b"#, options: .regularExpression) != nil {
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

    /// Distinct edited files named in a collapsed summary before overflow.
    static let editSummaryVisibleFiles = 2

    /// Observable activity header:
    /// `Edited timelineModel.ts +4 −1 · +1 more · Read 14 · Ran 1`.
    ///
    /// Edits name their files instead of collapsing to a bare count — a file
    /// change is the work product and must be legible without expanding. Must
    /// stay byte-identical to `formatActivitySummary()` on web; both are locked
    /// by `config/shell-activity-summary-fixtures.json`.
    static func activitySummary(for calls: [ActivityCall]) -> String {
        var counts = ["search": 0, "read": 0, "list": 0, "view": 0,
                      "edit": 0, "call": 0, "run": 0, "wait": 0]
        var runOperations: [String: (label: String, count: Int)] = [:]
        var runOperationOrder: [String] = []
        // Deduplicated by path, first-seen order. A file list is a set, so the
        // shell first-plus-last bracket does not apply: `A, B, A` must not
        // render `A · A`.
        var editFiles: [String: String] = [:]
        var editFileOrder: [String] = []
        var unnamedEdits = 0
        var unnamedRuns = 0
        let nouns: [String: (singular: String, plural: String)] = [
            "search": ("search", "searches"),
            "read": ("file", "files"),
            "list": ("directory", "directories"),
            "view": ("page", "pages"),
            "edit": ("file", "files"),
            "call": ("call", "calls"),
            "run": ("command", "commands"),
            "wait": ("time", "times"),
        ]
        func lowerFirst(_ text: String) -> String {
            guard let first = text.first else { return text }
            return first.lowercased() + text.dropFirst()
        }
        func joinWithAnd(_ parts: [String]) -> String {
            switch parts.count {
            case 0: return ""
            case 1: return parts[0]
            case 2: return "\(parts[0]) and \(parts[1])"
            default: return "\(parts.dropLast().joined(separator: ", ")) and \(parts[parts.count - 1])"
            }
        }
        for call in calls {
            let aggregate = shellSalience(call: call.call, result: call.result)?.aggregate
                ?? presentationAggregate(call.call)
            switch aggregate {
            case .search: counts["search", default: 0] += 1
            case .read: counts["read", default: 0] += 1
            case .list: counts["list", default: 0] += 1
            case .wait: counts["wait", default: 0] += 1
            case .none:
                let identity = ([presentedToolName(call.call), call.call.toolPresentation?.label]
                    .compactMap { $0 }).joined(separator: " ").lowercased()
                    .replacingOccurrences(of: #"[^a-z0-9]+"#, with: " ", options: .regularExpression)
                if isEditInteraction(call.call) {
                    counts["edit", default: 0] += 1
                    let stat = EditSummary.stat(for: call.call)
                    guard let label = EditSummary.format(stat), let path = stat.filePath else {
                        unnamedEdits += 1
                        continue
                    }
                    // First write of a path wins its label; later edits fold in.
                    if editFiles[path] == nil {
                        editFileOrder.append(path)
                        editFiles[path] = label
                    }
                } else if identity.range(of: #"\b(web|browser|fetch|view url|open url|read url|webfetch|websearch|searchweb|readurlcontent)\b"#, options: .regularExpression) != nil {
                    counts["view", default: 0] += 1
                } else if call.call.toolPresentation?.mcpNamespace != nil
                    || identity.range(of: #"\b(agent|task|subagent|invoke|called)\b"#, options: .regularExpression) != nil {
                    counts["call", default: 0] += 1
                } else {
                    guard let summary = call.call.toolPresentation?.shellSummary,
                          summary.confidence != "opaque",
                          !summary.operations.isEmpty else {
                        unnamedRuns += 1
                        continue
                    }
                    for operation in summary.operations {
                        let count = max(1, operation.count)
                        if let existing = runOperations[operation.key] {
                            runOperations[operation.key] = (existing.label, existing.count + count)
                        } else {
                            runOperationOrder.append(operation.key)
                            runOperations[operation.key] = (operation.label, count)
                        }
                    }
                }
            }
        }
        let ordered = [("search", "Searched"), ("read", "Read"), ("list", "Listed"),
                       ("view", "Viewed"), ("edit", "Edited"), ("call", "Called"),
                       ("run", "Ran"), ("wait", "Waited")]
        var parts = ordered.compactMap { key, label -> String? in
            if key == "run" || key == "wait" { return nil }
            let count = counts[key, default: 0]
            guard count > 0 else { return nil }
            guard key == "edit" else {
                let noun = nouns[key] ?? (singular: "item", plural: "items")
                return "\(label) \(count) \(count == 1 ? noun.singular : noun.plural)"
            }
            let labels = editFileOrder.compactMap { editFiles[$0] }
            guard !labels.isEmpty else { return "\(label) \(count)" }
            var visible = Array(labels.prefix(editSummaryVisibleFiles))
            let hidden = labels.count - visible.count + unnamedEdits
            if hidden > 0 { visible.append("+\(hidden) more") }
            return "\(label) \(visible.joined(separator: " · "))"
        }
        let operations = runOperationOrder.compactMap { runOperations[$0] }
        if operations.isEmpty {
            if unnamedRuns > 0 {
                let noun = unnamedRuns == 1 ? "command" : "commands"
                parts.append("Ran \(unnamedRuns) \(noun)")
            }
        } else {
            let visibleOperations = operations.count > 2
                ? [operations[0], operations[operations.count - 1]]
                : operations
            var visible = visibleOperations.map { operation in
                operation.count > 1 ? "\(operation.label) ×\(operation.count)" : operation.label
            }
            let hiddenDistinct = operations.count - visible.count
            if hiddenDistinct > 0 { visible.append("\(hiddenDistinct) more") }
            if unnamedRuns > 0 { visible.append("\(unnamedRuns) other") }
            parts.append("Ran \(joinWithAnd(visible))")
        }
        if counts["wait", default: 0] > 0 {
            let count = counts["wait", default: 0]
            parts.append("Waited \(count) \(count == 1 ? "time" : "times")")
        }
        return parts.enumerated().map { index, part in
            index == 0 ? part : lowerFirst(part)
        }.joined(separator: ", ")
    }

    static let explorationOverflowVisible = 8

    static func splitExplorationOverflow<T>(_ items: [T], visible: Int = explorationOverflowVisible) -> (earlier: [T], latest: [T]) {
        guard items.count > visible else { return ([], items) }
        let idx = items.count - visible
        return (Array(items.prefix(idx)), Array(items.suffix(visible)))
    }

    /// Build a paired, renderable timeline from raw events.
    /// Mirrors the web pairing logic: assistant-with-tool_name registers in a
    /// Map<tool_call_id, item>. Role=tool events look up their tool_call_id
    /// and attach as the result. Orphan tool events render as their own row.
    ///
    /// After pairing, consecutive passive tool calls are collapsed into a
    /// single `.activityGroup` row. User/assistant prose and ineligible calls
    /// are boundaries that flush the buffer.
    static func build(events: [SessionEvent]) -> [TimelineItem] {
        var raw: [TimelineItem] = []
        var callIdToIndex: [String: Int] = [:]
        var fifoToolCallIndexes: [Int] = []
        // A head index, not removeFirst(): that shifted the whole array for
        // every unkeyed result, quadratic in a long tool-heavy transcript.
        var fifoHead = 0
        /// Indexes of rows that absorbed a final-answer tool call. Their tool
        /// result is a bare ack with nothing left to render.
        var answerIndexes: Set<Int> = []

        for event in events {
            if event.interactionKind == "provider_notification" {
                raw.append(.providerNotification(event))
                continue
            }
            if event.role == "system" { continue }

            switch event.role {
            case "user":
                if case .user(let previous)? = raw.last, Self.isUserMessageFragment(base: previous.id, fragment: event.id) {
                    raw[raw.count - 1] = .user(
                        previous.withContentText(Self.joinMessageParts(previous.contentText, event.contentText))
                    )
                    continue
                }
                raw.append(.user(event))

            case "assistant":
                let hasText = !(event.contentText ?? "").isEmpty
                let hasTool = (event.toolName ?? "").isEmpty == false

                // A schema-constrained agent returns by calling a final-answer
                // tool: the input is the answer, the result is a bare ack.
                // Project the payload into the closing assistant message so a
                // finished session does not read as truncated mid-tool-call.
                if hasTool, let answer = Self.finalAnswerText(for: event) {
                    let prose = (event.contentText ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                    raw.append(.assistant(event.withContentText(prose.isEmpty ? answer : prose + "\n\n" + answer)))
                    answerIndexes.insert(raw.count - 1)
                    if let callId = event.toolCallId, !callId.isEmpty {
                        callIdToIndex[callId] = raw.count - 1
                    } else {
                        fifoToolCallIndexes.append(raw.count - 1)
                    }
                    continue
                }

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
                   fifoHead < fifoToolCallIndexes.count {
                    matchedIndex = fifoToolCallIndexes[fifoHead]
                    fifoHead += 1
                    resultPairing = .fifo
                }

                if let idx = matchedIndex, answerIndexes.contains(idx) {
                    // Ack for a final-answer call: the answer is already rendered.
                    continue
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

        return collapseActivity(raw)
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

    /// Pi/OMP store one user message as several content blocks, and the engine
    /// used to emit each block as its own user event — a pasted screenshot
    /// rendered the same prompt as two `you` rows. A fragment keeps the
    /// parent's native id with an `-image-<n>` / `-text-<n>` suffix. Merging it
    /// back is what the terminal showed: one prompt, one row.
    private static let userMessageFragmentKinds: Set<String> = ["image", "text"]

    static func isUserMessageFragment(base: String, fragment: String) -> Bool {
        let prefix = base + "-"
        guard fragment.hasPrefix(prefix) else { return false }
        let parts = fragment.dropFirst(prefix.count).split(separator: "-")
        guard parts.count == 2, userMessageFragmentKinds.contains(String(parts[0])) else { return false }
        return Int(parts[1]) != nil
    }

    static func joinMessageParts(_ previous: String?, _ next: String?) -> String {
        // Rows written before the engine stopped quoting the provider pointer
        // still carry it; no client can resolve it, so the presentation drops
        // the suffix and keeps the readable marker.
        func cleaned(_ text: String?) -> String {
            (text ?? "")
                .replacingOccurrences(
                    of: "; unsupported media reference: blob:sha256:[0-9a-f]{64}",
                    with: "",
                    options: .regularExpression
                )
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }
        let left = cleaned(previous)
        let right = cleaned(next)
        if left.isEmpty { return right }
        if right.isEmpty { return left }
        return left + "\n\n" + right
    }

    /// Collapse runs of 2+ completed calls into one prose-bounded activity row.
    static func collapseActivity(_ items: [TimelineItem]) -> [TimelineItem] {
        var out: [TimelineItem] = []
        var buffer: [ActivityCall] = []

        func flush() {
            guard !buffer.isEmpty else { return }
            if buffer.count == 1 {
                let only = buffer[0]
                out.append(.tool(call: only.call, result: only.result, pairing: only.pairing))
            } else {
                out.append(.activityGroup(calls: buffer))
            }
            buffer.removeAll()
        }

        for item in items {
            switch item {
            case .tool(let call, let result, let pairing)
                where Self.isActivityEligible(call: call, result: result, pairing: pairing):
                buffer.append(ActivityCall(call: call, result: result, pairing: pairing))
            default:
                flush()
                out.append(item)
            }
        }
        flush()
        return out
    }

    /// Markdown for a final-answer tool call, or nil for an ordinary tool call
    /// (or a final-answer call carrying no payload yet, which still deserves a
    /// visible in-flight tool row).
    static func finalAnswerText(for event: SessionEvent) -> String? {
        let tool = presentedToolName(event)
        guard ToolTiers.isFinalAnswerTool(tool) else { return nil }
        return FinalAnswer.format(event.toolInputValue)
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
