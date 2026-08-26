import Foundation

/// Rendering for final-answer tool calls. Mirrors
/// `web/src/lib/sessionWorkspace/finalAnswer.ts` — parity is enforced by
/// `tests/fixtures/session-projection/structured-output-final-answer.json`.
///
/// A schema-constrained agent (a Claude subagent or workflow step) does not end
/// its turn with prose — it ends it by calling a return tool whose input IS the
/// answer, and whose result is a bare ack. Rendered as an ordinary tool row that
/// reads as a session truncated mid-tool-call: an opaque chip carrying the
/// internal tool name and no payload, exactly where the deliverable belongs.
///
/// Keys are emitted in sorted order because Swift decodes JSON objects into an
/// unordered dictionary; sorting is the only ordering both clients can agree on.
enum FinalAnswer {
    /// Markdown for a final-answer tool call's input, or nil when there is
    /// nothing to show and the caller should keep rendering a tool row.
    static func format(_ input: JSONValue?) -> String? {
        guard let input else { return nil }

        var value = input
        if case .string(let raw) = input {
            let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            if text.isEmpty { return nil }
            if let data = text.data(using: .utf8),
               let parsed = try? JSONDecoder().decode(JSONValue.self, from: data),
               parsed.objectValue != nil {
                value = parsed
            } else {
                return text
            }
        }

        guard let record = value.objectValue else {
            if case .null = value { return nil }
            return fencedJSON(value)
        }

        let keys = record.keys.sorted()
        if keys.isEmpty { return nil }

        // A single string field is the whole answer; its key is scaffolding.
        if keys.count == 1, case .string(let only) = record[keys[0]]! {
            let text = only.trimmingCharacters(in: .whitespacesAndNewlines)
            return text.isEmpty ? nil : text
        }

        var blocks: [String] = []
        for key in keys {
            guard let field = record[key] else { continue }
            if case .null = field { continue }
            if case .array(let entries) = field, entries.isEmpty { continue }

            let body: String
            if let scalar = scalarText(field) {
                let trimmed = scalar.trimmingCharacters(in: .whitespacesAndNewlines)
                if trimmed.isEmpty { continue }
                body = trimmed
            } else if case .array(let entries) = field,
                      entries.allSatisfy({ scalarText($0) != nil }) {
                body = entries
                    .map { "- " + (scalarText($0) ?? "").trimmingCharacters(in: .whitespacesAndNewlines) }
                    .joined(separator: "\n")
            } else {
                body = fencedJSON(field)
            }
            blocks.append("**\(key)**\n\n\(body)")
        }

        return blocks.isEmpty ? nil : blocks.joined(separator: "\n\n")
    }

    private static func scalarText(_ value: JSONValue) -> String? {
        switch value {
        case .string(let text): return text
        case .int(let number): return String(number)
        case .double(let number): return numberLiteral(number)
        case .bool(let flag): return flag ? "true" : "false"
        default: return nil
        }
    }

    /// JSON number formatting that matches JavaScript's, so the TS and Swift
    /// renderings of the same payload stay byte-identical.
    private static func numberLiteral(_ value: Double) -> String {
        if value == value.rounded(), abs(value) < 1e15 {
            return String(Int64(value))
        }
        return String(value)
    }

    private static func fencedJSON(_ value: JSONValue) -> String {
        "```json\n" + stableJSON(value, indent: "") + "\n```"
    }

    /// Recursively key-sorted JSON, so web and iOS emit byte-identical blocks.
    private static func stableJSON(_ value: JSONValue, indent: String) -> String {
        switch value {
        case .null:
            return "null"
        case .bool(let flag):
            return flag ? "true" : "false"
        case .int(let number):
            return String(number)
        case .double(let number):
            return numberLiteral(number)
        case .string(let text):
            return encodeString(text)
        case .array(let entries):
            if entries.isEmpty { return "[]" }
            let inner = indent + "  "
            let body = entries.map { inner + stableJSON($0, indent: inner) }.joined(separator: ",\n")
            return "[\n" + body + "\n" + indent + "]"
        case .object(let fields):
            let keys = fields.keys.sorted()
            if keys.isEmpty { return "{}" }
            let inner = indent + "  "
            let body = keys
                .map { "\(inner)\(encodeString($0)): \(stableJSON(fields[$0]!, indent: inner))" }
                .joined(separator: ",\n")
            return "{\n" + body + "\n" + indent + "}"
        }
    }

    private static func encodeString(_ value: String) -> String {
        var out = "\""
        for character in value.unicodeScalars {
            switch character {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            default:
                if character.value < 0x20 {
                    out += String(format: "\\u%04x", character.value)
                } else {
                    out.unicodeScalars.append(character)
                }
            }
        }
        return out + "\""
    }
}

extension SessionEvent {
    /// Same durable event rendered as prose. The stored event is never mutated;
    /// this is the Swift half of the read-time projection that turns a
    /// final-answer tool call into the closing assistant message.
    func withContentText(_ text: String) -> SessionEvent {
        SessionEvent(
            id: id,
            role: role,
            contentText: text,
            toolName: nil,
            toolInputJSON: nil,
            toolInputValue: nil,
            toolOutputText: nil,
            toolCallId: nil,
            toolCallState: toolCallState,
            toolPresentation: nil,
            timestamp: timestamp,
            inActiveContext: inActiveContext,
            isHeadBranch: isHeadBranch,
            inputOrigin: inputOrigin,
            eventOrigin: eventOrigin,
            mediaRefs: mediaRefs,
            cursor: cursor,
            orderTimeUs: orderTimeUs,
            threadId: threadId,
            branchKind: branchKind
        )
    }
}
